from __future__ import annotations

import hashlib
import json
from typing import Self

from pydantic import Field, JsonValue, model_validator

from ouro.core.base import OuroModel, VersionedOuroModel, new_id, utc_now
from ouro.core.compaction import CompactionRecord
from ouro.core.errors import PersistenceError
from ouro.core.events import EventRecord
from ouro.core.harness import HarnessState, HarnessVersion
from ouro.core.message_frames import MessageTruncation
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord


class SessionCommitConflict(PersistenceError):
    """The operation's read state no longer permits its proposed effects."""


class HistoryBoundaryUnavailable(PersistenceError):
    """The selected message has no provable before-state checkpoint."""


class SessionCommitStep(OuroModel):
    """One logical message boundary and the business state it produces."""

    message: MessageRecord | None = None
    harness: HarnessState | None = None

    @model_validator(mode="after")
    def require_effect(self) -> Self:
        if self.message is None and self.harness is None:
            raise ValueError("a Session commit step must have an effect")
        return self


class SessionCommit(OuroModel):
    """A finite proposal evaluated against the Session observed by its caller."""

    operation_id: str = Field(
        default_factory=lambda: new_id("op"), pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
    )
    before: SessionRecord
    after: SessionRecord | None = None
    steps: tuple[SessionCommitStep, ...] = ()
    runs: tuple[RunRecord, ...] = ()
    events: tuple[EventRecord, ...] = ()
    output: dict[str, JsonValue] = Field(default_factory=dict)
    before_message_id: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        session_id = self.before.session_id
        records: list[RunRecord | EventRecord] = [*self.runs, *self.events]
        if self.after is not None and self.after.session_id != session_id:
            raise ValueError("Session commit cannot change Session identity")
        if any(record.session_id != session_id for record in records):
            raise ValueError("Session commit contains foreign execution records")
        for step in self.steps:
            if step.message is not None and step.message.session_id != session_id:
                raise ValueError("Session commit contains a foreign message")
            if step.harness is not None and any(
                item.session_id != session_id for item in step.harness.states
            ):
                raise ValueError("Session commit contains foreign Harness state")
        if self.before_message_id is not None and self.steps:
            raise ValueError("history withdrawal cannot also append new effects")
        return self

    @property
    def digest(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SessionCommitResult(VersionedOuroModel):
    operation_id: str
    request_digest: str
    session: SessionRecord
    messages: tuple[MessageRecord, ...] = ()
    events: tuple[EventRecord, ...] = ()
    output: dict[str, JsonValue] = Field(default_factory=dict)
    truncation: MessageTruncation | None = None


class PreparedSessionCommit(VersionedOuroModel):
    """The durable decision; applying it never executes plugin code."""

    result: SessionCommitResult
    versions: tuple[HarnessVersion, ...] = ()
    runs: tuple[RunRecord, ...] = ()
    compactions: tuple[CompactionRecord, ...] | None = None
    invalidate_context: bool = False


class HarnessMigration(OuroModel):
    """Explicit maintenance replacement of reachable checkpoints and their references."""

    operation_id: str = Field(
        default_factory=lambda: new_id("migration"), pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
    )
    before: SessionRecord
    replacements: dict[str, HarnessVersion] = Field(min_length=1)

    @property
    def digest(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def prepare_harness_migration(
    request: HarnessMigration,
    current: SessionRecord,
    messages: tuple[MessageRecord, ...],
) -> PreparedSessionCommit:
    """Preserve history and immutable source versions while changing only state references."""

    before = request.before
    if (
        current.session_id,
        current.harness_version,
        current.history_epoch,
        current.last_message_id,
    ) != (before.session_id, before.harness_version, before.history_epoch, before.last_message_id):
        raise SessionCommitConflict("Session changed while preparing Harness migration")
    references = {current.harness_version} | {
        item.before_harness_version for item in messages if item.before_harness_version is not None
    }
    for source, target in request.replacements.items():
        if (
            source not in references
            or target.session_id != current.session_id
            or target.version_id in references
        ):
            raise PersistenceError(
                "Harness migration must replace local reachable versions with new identities"
            )
    mapped = {source: target.version_id for source, target in request.replacements.items()}
    session = current.model_copy(deep=True)
    if current.harness_version in request.replacements:
        target = request.replacements[current.harness_version]
        session.harness_version = target.version_id
        session.controls = target.state.controls.model_copy(deep=True)
    session.history_epoch += 1
    session.updated_at = utc_now()
    return PreparedSessionCommit(
        result=SessionCommitResult(
            operation_id=request.operation_id,
            request_digest=request.digest,
            session=session,
            messages=tuple(
                item.model_copy(
                    update={"before_harness_version": mapped[item.before_harness_version]}
                )
                if item.before_harness_version in mapped
                else item
                for item in messages
            ),
            output={"harness_migration": {source: target for source, target in mapped.items()}},
        ),
        versions=tuple(request.replacements.values()),
        invalidate_context=True,
    )


def prepare_session_commit(
    request: SessionCommit,
    current: SessionRecord,
    harness: HarnessVersion,
    *,
    event_sequence: int,
    truncation: MessageTruncation | None = None,
    restore: HarnessVersion | None = None,
    compactions: tuple[CompactionRecord, ...] | None = None,
) -> PreparedSessionCommit:
    """Resolve ordered effects and optimistic preconditions before any write."""

    before = request.before
    if current.session_id != before.session_id or harness.session_id != current.session_id:
        raise SessionCommitConflict("Session commit identity changed")
    if current.history_epoch != before.history_epoch:
        raise SessionCommitConflict("Session history changed; operation is no longer valid")
    if current.harness_version != before.harness_version:
        raise SessionCommitConflict("Harness state changed; reload before committing")
    if harness.version_id != current.harness_version:
        raise PersistenceError("current Harness reference does not match its checkpoint")
    if current.last_message_id != before.last_message_id:
        raise SessionCommitConflict("Session message head changed; reload before committing")

    session = _merge_session(current, before, request.after)
    versions: list[HarnessVersion] = []
    messages: list[MessageRecord] = []
    state_changed = False
    for step in request.steps:
        previous_version = harness.version_id
        if step.harness is not None and not harness.state.same_value(step.harness):
            harness = HarnessVersion(session_id=current.session_id, state=step.harness)
            versions.append(harness)
            state_changed = True
        if step.message is not None:
            message = step.message.model_copy(
                deep=True,
                update={
                    "sequence": session.message_count + 1,
                    "before_harness_version": previous_version,
                },
            )
            messages.append(message)
            session.message_count += 1
            session.last_message_id = message.message_id

    if request.before_message_id is not None:
        if truncation is None or restore is None:
            raise HistoryBoundaryUnavailable("message has no recoverable Harness boundary")
        if truncation.first_removed.message_id != request.before_message_id:
            raise SessionCommitConflict("history cut does not match the requested message")
        if (
            restore.session_id != current.session_id
            or restore.version_id != truncation.first_removed.before_harness_version
        ):
            raise PersistenceError("history checkpoint does not belong to the selected boundary")
        harness = restore
        session.history_epoch += 1
        session.status = "idle"
        session.active_run_id = None
        session.last_run_id = None
        session.message_count = max(0, current.message_count - truncation.removed_count)
        session.last_message_id = (
            truncation.last_retained.message_id if truncation.last_retained is not None else None
        )
        if compactions is not None and session.active_compaction_id not in {
            item.compaction_id for item in compactions
        }:
            session.active_compaction_id = compactions[-1].compaction_id if compactions else None

    session.harness_version = harness.version_id
    session.controls = harness.state.controls.model_copy(deep=True)
    session.updated_at = request.after.updated_at if request.after is not None else utc_now()
    events = tuple(
        event.model_copy(update={"sequence": event_sequence + index + 1}, deep=True)
        for index, event in enumerate(request.events)
    )
    return PreparedSessionCommit(
        result=SessionCommitResult(
            operation_id=request.operation_id,
            request_digest=request.digest,
            session=session,
            messages=tuple(messages),
            events=events,
            output=request.output,
            truncation=truncation,
        ),
        versions=tuple(versions),
        runs=request.runs,
        compactions=compactions,
        # The latest context records the last prepared request. Appending its response
        # does not invalidate that fact; changing Harness or cutting history does.
        invalidate_context=state_changed or truncation is not None,
    )


def _merge_session(
    current: SessionRecord, before: SessionRecord, after: SessionRecord | None
) -> SessionRecord:
    result = current.model_copy(deep=True)
    if after is None:
        return result
    managed = {"controls", "harness_version", "history_epoch", "message_count", "last_message_id"}
    for name in SessionRecord.model_fields:
        if name in managed or name == "updated_at":
            continue
        old_value, new_value = getattr(before, name), getattr(after, name)
        if old_value == new_value:
            continue
        if getattr(current, name) != old_value:
            raise SessionCommitConflict(f"Session field changed during operation: {name}")
        setattr(result, name, new_value)
    return result.model_copy(deep=True)
