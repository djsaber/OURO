from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from ouro.core.base import OuroModel, VersionedOuroModel, new_id, utc_now
from ouro.core.compaction import CompactionRecord, validate_compaction_sources
from ouro.core.content import media_resource_ids
from ouro.core.context_selection import validate_media_selection
from ouro.core.context_snapshots import ModelRequestHeader, RequestManifestRecord
from ouro.core.errors import CompactionStateError, PersistenceError
from ouro.core.events import EventRecord
from ouro.core.harness import HarnessState, HarnessVersion
from ouro.core.inputs import InputRecord, InputState, InputStateChange
from ouro.core.message_frames import MessageTruncation
from ouro.core.messages import MessageFact, MessageRecord
from ouro.core.session_titles import fill_session_title
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord, SessionRecord
from ouro.core.tool_batches import ToolCallState
from ouro.core.tool_facts import ToolCallFacts


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
    before_message_id: str | None = None
    inputs: tuple[InputRecord, ...] = ()
    input_changes: tuple[InputStateChange, ...] = ()
    tool_facts: ToolCallFacts = Field(default_factory=ToolCallFacts)
    call_states: tuple[ToolCallState, ...] = ()
    compaction: CompactionRecord | None = None
    request_header: ModelRequestHeader | None = None
    request_manifest: RequestManifestRecord | None = None

    @property
    def media_resource_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                [
                    *media_resource_ids(
                        block
                        for step in self.steps
                        if step.message is not None
                        for block in step.message.content
                    ),
                    *media_resource_ids(
                        block
                        for outcome in self.tool_facts.outcomes
                        for block in outcome.value.content_blocks
                    ),
                    *(
                        self.request_manifest.manifest.referenced_resource_ids
                        if self.request_manifest is not None
                        else ()
                    ),
                ]
            )
        )

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        session_id = self.before.session_id
        if self.request_header is not None:
            if self.request_header.session_id != session_id:
                raise ValueError("Session commit contains a foreign request")
            if self.before_message_id is not None:
                raise ValueError("history withdrawal cannot also publish a request")
        if self.request_manifest is not None and (
            self.request_header is None
            or self.request_header.purpose != "agent"
            or self.request_manifest.context_id != self.request_header.context_id
        ):
            raise ValueError("request header and input have different identities")
        if self.compaction is not None and self.compaction.session_id != session_id:
            raise ValueError("Session commit contains a foreign compaction")
        if self.compaction is not None and self.before_message_id is not None:
            raise ValueError("history withdrawal cannot also activate a compaction")
        records: list[RunRecord | EventRecord] = [*self.runs, *self.events]
        if self.after is not None and self.after.session_id != session_id:
            raise ValueError("Session commit cannot change Session identity")
        if any(record.session_id != session_id for record in records):
            raise ValueError("Session commit contains foreign execution records")
        if any(record.session_id != session_id for record in self.inputs):
            raise ValueError("Session commit contains a foreign accepted input")
        if any(record.session_id != session_id for record in self.tool_facts.records):
            raise ValueError("Session commit contains foreign Call facts")
        if any(record.session_id != session_id for record in self.call_states):
            raise ValueError("Session commit contains foreign Call states")
        for step in self.steps:
            if step.message is not None and step.message.session_id != session_id:
                raise ValueError("Session commit contains a foreign message")
            if step.harness is not None and any(
                item.session_id != session_id for item in step.harness.states
            ):
                raise ValueError("Session commit contains foreign Harness state")
        if self.before_message_id is not None and self.steps:
            raise ValueError("history withdrawal cannot also append new effects")
        if self.before_message_id is not None and (self.tool_facts.calls or self.tool_facts.starts):
            raise ValueError("history withdrawal cannot accept or start a Call")
        return self


class SessionCommitResult(OuroModel):
    """Transient result of this write; never retained as a historical receipt."""

    operation_id: str
    session: SessionRecord
    messages: tuple[MessageFact, ...] = ()
    events: tuple[EventRecord, ...] = ()
    truncation: MessageTruncation | None = None


class PreparedSessionCommit(VersionedOuroModel):
    """The durable decision; applying it never executes plugin code."""

    result: SessionCommitResult
    versions: tuple[HarnessVersion, ...] = ()
    runs: tuple[RunRecord, ...] = ()
    updated_compactions: tuple[CompactionRecord, ...] = ()
    removed_compaction_ids: tuple[str, ...] = ()
    compaction: CompactionRecord | None = None
    invalidate_context: bool = False
    clear_response_draft: bool = False
    request_header: ModelRequestHeader | None = None
    inputs: tuple[InputRecord, ...] = ()
    input_states: tuple[InputState, ...] = ()
    tool_facts: ToolCallFacts = Field(default_factory=ToolCallFacts)
    call_states: tuple[ToolCallState, ...] = ()


def prepare_session_commit(
    request: SessionCommit,
    current: SessionRecord,
    harness: HarnessVersion | None,
    *,
    event_sequence: int,
    truncation: MessageTruncation | None = None,
    restore: HarnessVersion | None = None,
    retained_compaction_ids: tuple[str, ...] | None = None,
    updated_compactions: tuple[CompactionRecord, ...] = (),
    removed_compaction_ids: tuple[str, ...] = (),
    input_states: tuple[InputState, ...] = (),
    withdrawn_input_ids: tuple[str, ...] = (),
    tool_facts: ToolCallFacts | None = None,
    compaction_sources: tuple[MessageFact, ...] = (),
    media_source: MessageFact | None = None,
    selected_compaction: CompactionRecord | None = None,
    pending_runs: tuple[RunRecord, ...] = (),
    current_request: ModelRequestHeader | None = None,
) -> PreparedSessionCommit:
    """Resolve effects; message/Harness steps need the full current checkpoint."""

    before = request.before
    if current.session_id != before.session_id or (
        harness is not None and harness.session_id != current.session_id
    ):
        raise SessionCommitConflict("Session commit identity changed")
    if current.history_epoch != before.history_epoch:
        raise SessionCommitConflict("Session history changed; operation is no longer valid")
    if current.harness_version != before.harness_version:
        raise SessionCommitConflict("Harness state changed; reload before committing")
    if harness is not None and harness.version_id != current.harness_version:
        raise PersistenceError("current Harness reference does not match its checkpoint")
    if current.last_message_id != before.last_message_id:
        raise SessionCommitConflict("Session message head changed; reload before committing")
    if request.request_header is not None:
        same_request = (
            current_request is not None
            and current_request.context_id == request.request_header.context_id
        )
        if request.request_manifest is not None and same_request:
            raise SessionCommitConflict("request input identity is already published")
        if (
            request.request_manifest is None
            and not same_request
            and not (current_request is None and request.request_header.purpose == "compaction")
        ):
            raise SessionCommitConflict("latest context does not match the request update")

    session = _merge_session(current, before, request.after)
    selected_id = session.context_selection.compaction_id
    if (
        request.compaction is None
        and selected_id is not None
        and selected_id != current.context_selection.compaction_id
        and (
            selected_compaction is None
            or selected_compaction.compaction_id != selected_id
            or selected_compaction.session_id != current.session_id
        )
    ):
        raise SessionCommitConflict("selected compaction does not belong to current history")
    if (
        session.context_selection.media_elided_through
        != current.context_selection.media_elided_through
    ):
        try:
            validate_media_selection(session.context_selection, current.session_id, media_source)
        except PersistenceError as exc:
            raise SessionCommitConflict(str(exc)) from exc
    if (compaction := request.compaction) is not None:
        if current.context_selection.compaction_id != compaction.previous_compaction_id:
            raise SessionCommitConflict("active compaction changed during summary generation")
        sources = {item.message_id: item for item in compaction_sources}
        try:
            validate_compaction_sources(
                compaction, current.session_id, current.message_count, sources
            )
        except CompactionStateError as exc:
            raise SessionCommitConflict(str(exc)) from exc
        session.context_selection.compaction_id = compaction.compaction_id
    versions: list[HarnessVersion] = []
    messages: list[MessageRecord] = []
    runs = {run.run_id: run for run in request.runs}
    for step in request.steps:
        assert harness is not None
        previous_version = harness.version_id
        if step.harness is not None and not harness.state.same_value(step.harness):
            harness = HarnessVersion(session_id=current.session_id, state=step.harness)
            versions.append(harness)
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
        for pending in pending_runs:
            run = runs.get(pending.run_id, pending)
            if (
                run.history_epoch == current.history_epoch
                and run.continuation is not None
                and run.continuation.state == "pending"
            ):
                runs[run.run_id] = run.model_copy(
                    update={
                        "continuation": run.continuation.resolved(
                            action="pause", reason="session_history_withdrawn"
                        )
                    }
                )
        session.history_epoch += 1
        session.status = "idle"
        session.active_run_id = None
        session.last_run_id = None
        session.message_count = max(0, current.message_count - truncation.removed_count)
        session.last_message_id = (
            truncation.last_retained.message_id if truncation.last_retained is not None else None
        )
        session.context_selection = session.context_selection.retain_prefix(
            truncation.last_retained
        )
        if (
            retained_compaction_ids is not None
            and session.context_selection.compaction_id is not None
            and session.context_selection.compaction_id not in retained_compaction_ids
        ):
            session.context_selection.compaction_id = (
                retained_compaction_ids[-1] if retained_compaction_ids else None
            )

    changed_inputs = _prepare_inputs(
        request, session, messages, input_states, withdrawn_input_ids=withdrawn_input_ids
    )
    session = fill_session_title(session, messages)
    session.last_commit_id = request.operation_id
    if harness is not None:
        session.harness_version = harness.version_id
        session.controls = harness.state.controls.model_copy(deep=True)
    if session.continuation_request is not None and not session.continuation_request.is_current(
        session
    ):
        session.continuation_request = None
    session.updated_at = request.after.updated_at if request.after is not None else utc_now()
    proposed_events = list(request.events)
    response = next(
        (
            message
            for message in messages
            if current_request is not None
            and message.role == "assistant"
            and message.run_id == current_request.run_id
            and message.turn_id == current_request.turn_id
        ),
        None,
    )
    if response is not None and current_request is not None:
        proposed_events.insert(
            0,
            EventRecord(
                session_id=session.session_id,
                run_id=response.run_id,
                turn_id=response.turn_id,
                event_type="model_response_committed",
                payload={
                    "context_id": current_request.context_id,
                    "message_id": response.message_id,
                },
            ),
        )
    events = tuple(
        event.model_copy(
            update={
                "sequence": event_sequence + index + 1,
                "history_epoch": next(
                    (run.history_epoch for run in request.runs if run.run_id == event.run_id),
                    session.history_epoch,
                ),
                "metadata": {**event.metadata, "operation_id": request.operation_id},
            },
            deep=True,
        )
        for index, event in enumerate(proposed_events)
    )
    request_header = request.request_header
    response_header = request_header or current_request
    if response_header is not None:
        for message in messages:
            if (
                message.role == "assistant"
                and message.run_id == response_header.run_id
                and message.turn_id == response_header.turn_id
            ):
                if response_header.response_message_id not in {None, message.message_id}:
                    raise SessionCommitConflict("request response is already committed")
                request_header = response_header = response_header.model_copy(
                    update={
                        "response_message_id": message.message_id,
                        "response_closed_at": utc_now(),
                    }
                )
        if response_header.response_closed_at is None and any(
            run.run_id == response_header.run_id and run.status not in ACTIVE_RUN_STATUSES
            for run in request.runs
        ):
            request_header = response_header = response_header.model_copy(
                update={"response_closed_at": utc_now()}
            )
    return PreparedSessionCommit(
        result=SessionCommitResult(
            operation_id=request.operation_id,
            session=session,
            messages=tuple(message.as_fact() for message in messages),
            events=events,
            truncation=truncation,
        ),
        versions=tuple(versions),
        runs=tuple(runs.values()),
        tool_facts=tool_facts or ToolCallFacts(),
        call_states=request.call_states,
        updated_compactions=updated_compactions,
        removed_compaction_ids=removed_compaction_ids,
        compaction=request.compaction,
        request_header=request_header,
        clear_response_draft=(
            truncation is not None
            or request.request_manifest is not None
            or (
                request_header is not None
                and request_header.purpose == "agent"
                and request_header.response_closed_at is not None
            )
        ),
        # The latest context records the last prepared request. Appending its response
        # and updating Harness state do not invalidate that fact. History withdrawal does.
        invalidate_context=truncation is not None,
        inputs=request.inputs,
        input_states=changed_inputs,
    )


def _prepare_inputs(
    request: SessionCommit,
    session: SessionRecord,
    messages: list[MessageRecord],
    current_states: tuple[InputState, ...],
    *,
    withdrawn_input_ids: tuple[str, ...],
) -> tuple[InputState, ...]:
    states = {state.input_id: state for state in current_states}
    if any(state.session_id != session.session_id for state in current_states):
        raise PersistenceError("input state belongs to another Session")
    changed: dict[str, InputState] = {}
    pending = list(session.pending_input_ids)
    for record in request.inputs:
        identity = record.item.queue_id
        if identity in states:
            raise SessionCommitConflict("input identity is already accepted")
        if record.item.history_epoch != request.before.history_epoch:
            raise SessionCommitConflict("input belongs to withdrawn Session history")
        state = InputState.pending(record)
        states[identity] = changed[identity] = state
        pending.append(identity)
    if len({change.input_id for change in request.input_changes}) != len(request.input_changes):
        raise SessionCommitConflict("input has more than one transition in a decision")
    message_ids = {message.message_id for message in messages}
    message_inputs = {message.message_id: message.input_id for message in messages}
    consumption = {
        change.input_id: set(change.message_ids)
        for change in request.input_changes
        if change.status == "consumed"
    }
    for message in messages:
        reference = message.input_ref
        if reference is None:
            continue
        input_state = states.get(reference.input_id)
        if input_state is None or message.message_id not in consumption.get(
            reference.input_id, set()
        ):
            raise SessionCommitConflict(
                "Input message must be published by its consumption decision"
            )
        expected_semantics = (
            (input_state.role, input_state.scaffold, input_state.semantic_tag)
            if reference.part == "body"
            else ("user", True, "attachments")
        )
        if (message.role, message.scaffold, message.semantic_tag) != expected_semantics or (
            reference.part == "attachments" and input_state.source != "user"
        ):
            raise SessionCommitConflict("message semantics do not match its accepted Input")
    for change in request.input_changes:
        current = states.get(change.input_id)
        if current is None or current.status != "pending":
            raise SessionCommitConflict(f"input is no longer pending: {change.input_id}")
        if change.input_id not in pending:
            raise PersistenceError("pending input is missing from the Session queue")
        if not set(change.message_ids) <= message_ids:
            raise SessionCommitConflict("input consumption must publish its messages together")
        if any(message_inputs[identity] != change.input_id for identity in change.message_ids):
            raise SessionCommitConflict("input consumption references another input's messages")
        changed[change.input_id] = current.model_copy(
            update={
                "status": change.status,
                "message_ids": change.message_ids or current.message_ids,
            }
        )
        pending = [identity for identity in pending if identity != change.input_id]
    for identity in withdrawn_input_ids:
        current = states.get(identity)
        if current is not None and current.status == "consumed":
            changed[identity] = current.model_copy(update={"status": "withdrawn"})
    if request.before_message_id is not None:
        for identity in tuple(pending):
            pending_state = states.get(identity)
            if pending_state is None:
                raise PersistenceError(f"pending input state not found: {identity}")
            if pending_state.history_bound or pending_state.source in {"plugin", "delegation"}:
                changed[identity] = pending_state.model_copy(update={"status": "cancelled"})
                pending.remove(identity)
    session.pending_input_ids = tuple(pending)
    return tuple(changed.values())


def _merge_session(
    current: SessionRecord, before: SessionRecord, after: SessionRecord | None
) -> SessionRecord:
    result = current.model_copy(deep=True)
    if after is None:
        return result
    managed = {
        "controls",
        "harness_version",
        "history_epoch",
        "message_count",
        "run_count",
        "resource_count",
        "last_message_id",
        "pending_input_ids",
        "last_commit_id",
    }
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
