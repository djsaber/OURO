from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Literal

from pydantic import Field

from ouro.core.agents import DelegationRecord
from ouro.core.base import OuroModel
from ouro.core.compaction import (
    CompactionRecord,
    retain_compaction_references,
)
from ouro.core.errors import PersistenceError
from ouro.core.harness import HarnessVersion
from ouro.core.message_content import validate_tool_message
from ouro.core.message_frames import MessageTruncation
from ouro.core.message_statistics import RunMessageCounts
from ouro.core.messages import MessageFact
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.resources import SessionResourceReference
from ouro.core.session_commits import (
    HistoryBoundaryUnavailable,
    PreparedSessionCommit,
    SessionCommit,
    SessionCommitConflict,
    SessionCommitResult,
    prepare_session_commit,
)
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord, SessionRecord
from ouro.core.tool_batches import validate_call_states
from ouro.core.tool_facts import ToolCallFacts, ToolOutcomeRecord
from ouro.local_io import read_text_atomic, write_model_atomic
from ouro.sessions.backends.files.compactions import (
    has_compaction,
    read_compaction,
)
from ouro.sessions.backends.files.contexts import input_path
from ouro.sessions.backends.files.database import connect_data
from ouro.sessions.backends.files.delegations import (
    FileDelegationDecision,
)
from ouro.sessions.backends.files.delegations import (
    find_record as find_delegation,
)
from ouro.sessions.backends.files.delegations import (
    lookup_path as delegation_lookup_path,
)
from ouro.sessions.backends.files.delegations import (
    record_path as delegation_path,
)
from ouro.sessions.backends.files.delegations import (
    write_decision as write_delegation,
)
from ouro.sessions.backends.files.inputs import input_path as accepted_input_path
from ouro.sessions.backends.files.message_locations import FileMessageReference
from ouro.sessions.backends.files.messages import FileMessageRecords
from ouro.sessions.sqlite_compactions import (
    insert_compaction,
    read_references,
    update_compaction_boundary,
)
from ouro.sessions.sqlite_connections import ExecutionConnections
from ouro.sessions.sqlite_drafts import publish_draft, retire_draft
from ouro.sessions.sqlite_facts import load_required, write_call_state, write_facts
from ouro.sessions.sqlite_inputs import read_state, write_state
from ouro.sessions.sqlite_observation import advance_revision, read_revision
from ouro.sessions.sqlite_requests import read_latest_header, read_request_header, write_header
from ouro.sessions.sqlite_resources import attach_reference, insert_reference
from ouro.sessions.sqlite_runs import read_pending_runs, read_run, write_run
from ouro.sessions.sqlite_sessions import read_session, write_session


class FileCommitDecision(PreparedSessionCommit):
    kind: Literal["session"] = "session"
    message_offset: int = Field(ge=0)
    obsolete_context_id: str | None = None
    published_context_id: str | None = None
    removed_messages: tuple[FileMessageReference, ...] = ()
    message_references: tuple[FileMessageReference, ...] = ()
    message_counts: tuple[RunMessageCounts, ...] = ()
    resources: tuple[SessionResourceReference, ...] = ()


type FileDecision = FileCommitDecision | FileDelegationDecision


class FileSessionCommits:
    """Apply one Session decision under the adapter's existing mutation lock."""

    def __init__(
        self,
        directory: Callable[[str], Path],
        connections: Mapping[str, ExecutionConnections] | None = None,
    ) -> None:
        self.directory = directory
        self.connections = connections if connections is not None else {}
        self.messages = FileMessageRecords()

    def connect(
        self, session_id: str, *, mode: str = "ro"
    ) -> AbstractContextManager[sqlite3.Connection]:
        return connect_data(
            self.directory(session_id), mode=mode, connections=self.connections.get(session_id)
        )

    def commit_run(self, run: RunRecord, *, session: SessionRecord) -> None:
        with self.connect(run.session_id, mode="rw") as connection:
            revision = advance_revision(connection, run.session_id)
            if write_run(connection, run, history_epoch=session.history_epoch, revision=revision):
                session.run_count += 1
                write_session(connection, session)

    def commit_delegation(
        self, record: DelegationRecord, *, previous: DelegationRecord | None = None
    ) -> None:
        directory = self.directory(record.root_session_id)
        delegation_path(directory, record.delegation_id)
        delegation_lookup_path(directory, "parents", record.parent_run_id)
        lookups: tuple[tuple[Literal["requests", "children"], str | None], ...] = (
            ("requests", record.idempotency_key),
            ("children", record.child_session_id),
        )
        for kind, key in lookups:
            if key is not None:
                existing = find_delegation(directory, record.root_session_id, kind, key)
                if existing is not None and existing.delegation_id != record.delegation_id:
                    raise PersistenceError(f"Delegation {kind} identity already exists")
        with self.connect(record.root_session_id) as connection:
            revision = read_revision(connection, record.root_session_id) + 1
        decision = FileDelegationDecision(
            observation_revision=revision,
            record=record,
            old_parent_run_id=previous.parent_run_id if previous is not None else None,
            old_child_session_id=previous.child_session_id if previous is not None else None,
            old_idempotency_key=previous.idempotency_key if previous is not None else None,
        )
        self._publish_decision(record.root_session_id, decision)

    def load_session_record(self, session_id: str) -> SessionRecord:
        with self.connect(session_id) as connection:
            return read_session(connection, session_id)

    def load_harness(self, session_id: str, version_id: str) -> HarnessVersion:
        path = self._version_path(session_id, version_id)
        try:
            version = decode_versioned_record_json(
                HarnessVersion, read_text_atomic(path), label=f"Harness {version_id}"
            )
        except FileNotFoundError as exc:
            raise PersistenceError(f"Harness checkpoint not found: {version_id}") from exc
        if version.session_id != session_id or version.version_id != version_id:
            raise PersistenceError("Harness checkpoint has a foreign identity")
        return version

    def commit(
        self, request: SessionCommit, resources: tuple[SessionResourceReference, ...] = ()
    ) -> SessionCommitResult:
        session_id = request.before.session_id
        directory = self.directory(session_id)
        with self.connect(session_id) as connection:
            decision = self._prepare(connection, request)
        decision.resources = resources
        decision.result.session.resource_count += len(resources)
        for version in decision.versions:
            self.save_version(version)
        if request.request_manifest is not None:
            manifest = request.request_manifest
            content_path = input_path(directory, manifest.context_id)
            if content_path.exists():
                raise SessionCommitConflict("request input identity is already published")
            content_path.parent.mkdir(exist_ok=True)
            # Publish immutable input before the finite decision. The decision keeps
            # only its header, so replay never copies or parses the full input.
            write_model_atomic(content_path, manifest)
        if (
            decision.result.messages
            or decision.result.truncation is not None
            or decision.inputs
            or decision.versions
            or decision.published_context_id is not None
            or decision.obsolete_context_id is not None
        ):
            self._publish_decision(session_id, decision)
        else:
            # No external files participate in this decision.
            with self.connect(session_id, mode="rw") as connection:
                self._apply_native(connection, decision)
        return decision.result

    def _prepare(
        self, connection: sqlite3.Connection, request: SessionCommit
    ) -> FileCommitDecision:
        session_id = request.before.session_id
        directory = self.directory(session_id)
        current = read_session(connection, session_id)
        harness = self.load_harness(session_id, current.harness_version) if request.steps else None
        if current.last_commit_id == request.operation_id:
            raise SessionCommitConflict("commit already applied; query the domain result")
        message_offset = 0
        truncation = None
        restore = None
        compaction_references = None
        updated_compactions: tuple[CompactionRecord, ...] = ()
        removed_compaction_ids: tuple[str, ...] = ()
        removed_messages: tuple[FileMessageReference, ...] = ()
        if request.before_message_id is not None:
            truncation, message_offset, removed_messages = self._locate_cut(connection, request)
            version_id = truncation.first_removed.before_harness_version
            if version_id is None:
                raise HistoryBoundaryUnavailable("message has no Harness checkpoint")
            restore = self.load_harness(session_id, version_id)
            existing = read_references(connection, session_id)
            compaction_references = tuple(
                retain_compaction_references(
                    existing,
                    {item.message_id for item in removed_messages},
                    last_retained_message_id=(
                        truncation.last_retained.message_id if truncation.last_retained else None
                    ),
                )
            )
            retained_ids = {item.compaction_id for item in compaction_references}
            removed_compaction_ids = tuple(
                item.compaction_id for item in existing if item.compaction_id not in retained_ids
            )
            previous = {item.compaction_id: item for item in existing}
            updated_compactions = tuple(
                read_compaction(connection, directory, item.compaction_id).model_copy(
                    update={
                        "history_boundary_message_id": item.history_boundary_message_id,
                        "context_metrics_after": None,
                    }
                )
                for item in compaction_references
                if item != previous[item.compaction_id]
            )
        compaction_sources: tuple[MessageFact, ...] = ()
        if request.compaction is not None:
            if has_compaction(connection, directory, request.compaction.compaction_id):
                raise SessionCommitConflict("compaction identity already exists")
            compaction_sources = tuple(
                fact
                for identity in request.compaction.referenced_message_ids
                if (fact := self.messages.fact(connection, directory, identity)) is not None
            )
        media_source = None
        selected_compaction = None
        if (
            request.compaction is None
            and request.after is not None
            and (selected_id := request.after.context_selection.compaction_id) is not None
            and selected_id != current.context_selection.compaction_id
        ):
            try:
                selected_compaction = read_compaction(connection, directory, selected_id)
            except FileNotFoundError:
                pass
        if (
            request.after is not None
            and (boundary := request.after.context_selection.media_elided_through) is not None
            and boundary != current.context_selection.media_elided_through
        ):
            media_source = self.messages.fact(connection, directory, boundary.message_id)
        withdrawn_inputs = tuple(
            dict.fromkeys(item.queued_input_id for item in removed_messages if item.queued_input_id)
        )
        input_ids = dict.fromkeys(
            [item.item.queue_id for item in request.inputs]
            + [change.input_id for change in request.input_changes]
            + list(withdrawn_inputs)
            + (list(current.pending_input_ids) if request.before_message_id is not None else [])
        )
        input_states = tuple(
            state
            for identity in input_ids
            if (state := read_state(connection, session_id, identity)) is not None
        )
        latest_context = (
            read_latest_header(connection, session_id)
            if request.request_manifest is not None
            else None
        )
        current_request = (
            read_request_header(connection, session_id, request.request_header.context_id)
            if request.request_header is not None
            and (
                request.request_manifest is not None
                or request.request_header.purpose == "compaction"
            )
            else read_latest_header(connection, session_id)
            if request.request_header is not None
            or truncation is not None
            or any(run.status not in ACTIVE_RUN_STATUSES for run in request.runs)
            or any(
                step.message is not None and step.message.role == "assistant"
                for step in request.steps
            )
            else None
        )
        tool_facts, message_resources = self._prepare_tool_facts(connection, request, current)
        pending_runs = (
            tuple(read_pending_runs(connection, session_id, current.history_epoch))
            if truncation is not None
            else ()
        )
        new_run_count = sum(
            connection.execute(
                "SELECT 1 FROM runs WHERE session_id = ? AND run_id = ?",
                (session_id, run.run_id),
            ).fetchone()
            is None
            for run in request.runs
        )
        prepared = prepare_session_commit(
            request,
            current,
            harness,
            truncation=truncation,
            restore=restore,
            retained_compaction_ids=(
                tuple(item.compaction_id for item in compaction_references)
                if compaction_references is not None
                else None
            ),
            updated_compactions=updated_compactions,
            removed_compaction_ids=removed_compaction_ids,
            input_states=input_states,
            withdrawn_input_ids=withdrawn_inputs,
            tool_facts=tool_facts,
            compaction_sources=compaction_sources,
            media_source=media_source,
            selected_compaction=selected_compaction,
            current_request=current_request,
            pending_runs=pending_runs,
        )
        prepared.result.session.run_count += new_run_count
        references_list = []
        if prepared.result.messages and truncation is None:
            message_offset = (directory / "messages.jsonl").stat().st_size
        offset = message_offset
        first_position = prepared.result.session.message_count - len(prepared.result.messages) + 1
        for position, item in enumerate(prepared.result.messages, start=first_position):
            references_list.append(
                FileMessageReference.from_message(
                    item,
                    position=position,
                    offset=offset,
                    resource_refs=message_resources[item.message_id],
                )
            )
            offset += len(item.storage_json().encode("utf-8")) + 1
        references = tuple(references_list)
        decision = FileCommitDecision(
            **dict(prepared),
            message_offset=message_offset,
            removed_messages=removed_messages,
            message_references=references,
            message_counts=self.messages.index.prepare_counts(
                connection, references, removed_messages
            ),
            published_context_id=request.request_manifest.context_id
            if request.request_manifest is not None
            else None,
            obsolete_context_id=(
                latest_context.context_id
                if latest_context is not None
                else current_request.context_id
                if current_request is not None and prepared.invalidate_context
                else None
            ),
        )
        self._validate_appends(connection, decision)
        if any(
            accepted_input_path(directory, item.item.queue_id).exists() for item in request.inputs
        ):
            raise SessionCommitConflict("input identity is already accepted")
        return decision

    def _publish_decision(self, session_id: str, decision: FileDecision) -> None:
        write_model_atomic(self.directory(session_id) / "commit.json", decision)
        self._apply_decision(session_id, decision)

    def finish(self, session_id: str) -> None:
        directory = self.directory(session_id)
        path = directory / "commit.json"
        if not path.exists():
            return
        payload = read_text_atomic(path)
        try:
            kind = json.loads(payload)["kind"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PersistenceError("Session decision has no valid kind") from exc
        decision_types: dict[str, type[FileDecision]] = {
            "session": FileCommitDecision,
            "delegation": FileDelegationDecision,
        }
        decision_type = decision_types.get(kind)
        if decision_type is None:
            raise PersistenceError("unknown Session decision kind")
        decision = decode_versioned_record_json(
            decision_type, payload, label=f"Session decision {session_id}"
        )
        if (
            isinstance(decision, FileCommitDecision)
            and self.load_session_record(session_id).last_commit_id
            == decision.result.session.last_commit_id
        ):
            # The head and facts committed together. Only cross-file retirement
            # can remain after that transaction.
            if decision.obsolete_context_id is not None:
                input_path(directory, decision.obsolete_context_id).unlink(missing_ok=True)
            path.unlink()
        else:
            self._apply_decision(session_id, decision)

    def _apply_decision(self, session_id: str, decision: FileDecision) -> None:
        directory = self.directory(session_id)
        path = directory / "commit.json"
        if isinstance(decision, FileDelegationDecision):
            if decision.record.root_session_id != session_id:
                raise PersistenceError("Delegation decision has a foreign root identity")
            write_delegation(directory, decision)
            with self.connect(session_id, mode="rw") as connection:
                connection.execute(
                    "INSERT INTO session_revisions VALUES (?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET revision = excluded.revision",
                    (session_id, decision.observation_revision),
                )
            path.unlink()
            return
        result = decision.result
        if result.session.session_id != session_id:
            raise PersistenceError("Session decision has a foreign identity")
        # No callback or public store query runs while replaying a decision.
        # Replacing each journal suffix makes an interrupted append repeatable.
        for version in decision.versions:
            self.save_version(version)
        self.load_harness(session_id, result.session.harness_version)
        for record in decision.inputs:
            write_model_atomic(accepted_input_path(directory, record.item.queue_id), record)
        if result.messages or result.truncation is not None:
            self._write_suffix(
                directory / "messages.jsonl",
                decision.message_offset,
                result.messages,
            )
        with self.connect(session_id, mode="rw") as connection:
            self._apply_native(connection, decision)
        if decision.obsolete_context_id is not None:
            input_path(directory, decision.obsolete_context_id).unlink(missing_ok=True)
        path.unlink()

    def _apply_native(self, connection: sqlite3.Connection, decision: FileCommitDecision) -> None:
        result = decision.result
        session_id = result.session.session_id
        revision = advance_revision(connection, session_id)
        for state in decision.input_states:
            write_state(connection, state, revision=revision)
        self.messages.index.apply(
            connection,
            decision.message_references,
            decision.removed_messages,
            decision.message_counts,
        )
        write_facts(connection, decision.tool_facts.records)
        for call_state in decision.call_states:
            write_call_state(connection, call_state, revision=revision)
        for identity in decision.removed_compaction_ids:
            connection.execute(
                "DELETE FROM compactions WHERE session_id = ? AND compaction_id = ?",
                (session_id, identity),
            )
        for item in decision.updated_compactions:
            update_compaction_boundary(connection, item)
        if decision.compaction is not None:
            insert_compaction(connection, decision.compaction, revision=revision)
        if decision.obsolete_context_id is not None:
            retire_draft(connection, session_id, decision.obsolete_context_id)
        if decision.request_header is not None:
            write_header(connection, decision.request_header)
            publish_draft(connection, decision.request_header)
        if decision.invalidate_context:
            connection.execute("DELETE FROM latest_contexts WHERE session_id = ?", (session_id,))
        if decision.published_context_id is not None:
            connection.execute(
                "INSERT INTO latest_contexts VALUES (?, ?) ON CONFLICT(session_id) "
                "DO UPDATE SET context_id = excluded.context_id",
                (session_id, decision.published_context_id),
            )
        for run in decision.runs:
            if run.session_id != session_id:
                raise PersistenceError("Session decision contains a foreign Run")
            write_run(
                connection, run, history_epoch=result.session.history_epoch, revision=revision
            )
        if result.truncation is not None:
            connection.execute(
                "UPDATE runs SET needs_recovery = 0 WHERE session_id = ? "
                "AND history_epoch != ? AND needs_recovery = 1",
                (session_id, result.session.history_epoch),
            )
        write_session(connection, result.session)
        for reference in decision.resources:
            insert_reference(connection, session_id, reference)
        result.published_at(revision)

    def commit_outcome(
        self, outcome: ToolOutcomeRecord, resources: tuple[SessionResourceReference, ...] = ()
    ) -> ToolOutcomeRecord:
        outcome = ToolOutcomeRecord.model_validate(outcome.model_dump())
        proposed = ToolCallFacts(outcomes=(outcome,))
        with self.connect(outcome.session_id, mode="rw") as connection:
            prepared = proposed.prepare(
                session_id=outcome.session_id,
                history_epoch=0,
                active_runs={},
                existing=load_required(connection, outcome.session_id, proposed),
            )
            write_facts(connection, prepared.outcomes)
            if prepared.outcomes:
                advance_revision(connection, outcome.session_id)
            for reference in resources:
                attach_reference(connection, outcome.session_id, reference)
        return outcome

    def _prepare_tool_facts(
        self, connection: sqlite3.Connection, request: SessionCommit, current: SessionRecord
    ) -> tuple[ToolCallFacts, dict[str, tuple[str, ...]]]:
        proposed = request.tool_facts
        session_id = current.session_id
        messages = tuple(step.message for step in request.steps if step.message is not None)
        existing = load_required(connection, session_id, proposed, request.call_states, messages)
        active_runs = {}
        active_run_id = (
            request.after.active_run_id
            if request.after is not None
            and request.after.active_run_id != request.before.active_run_id
            else current.active_run_id
        )
        staged_runs = {run.run_id: run for run in request.runs}
        for run_id in dict.fromkeys(
            (*proposed.execution_run_ids, *(state.identity.run_id for state in request.call_states))
        ):
            run = staged_runs.get(run_id)
            if run is None:
                try:
                    run = read_run(connection, session_id, run_id)
                except FileNotFoundError:
                    continue
            if run.session_id != session_id or run.run_id != run_id:
                raise PersistenceError("Call Run has a foreign identity")
            if (
                run.run_id == active_run_id
                and run.status in ACTIVE_RUN_STATUSES
                and not run.abort_requested
            ):
                active_runs[run_id] = run.history_epoch
        prepared = proposed.prepare(
            session_id=session_id,
            history_epoch=current.history_epoch,
            active_runs=active_runs,
            existing=existing,
        )
        validate_call_states(
            request.call_states,
            ToolCallFacts(
                calls=existing.calls + prepared.calls,
                starts=existing.starts + prepared.starts,
                outcomes=existing.outcomes + prepared.outcomes,
            ),
            session_id=session_id,
            active_runs=active_runs,
            history_epoch=current.history_epoch,
        )
        calls = {call.identity: call for call in (*existing.calls, *prepared.calls)}
        outcomes = {
            (item.identity, item.attempt): item for item in (*existing.outcomes, *prepared.outcomes)
        }
        resources = {}
        for message in messages:
            reference = message.outcome_ref
            outcome = (
                outcomes.get((reference.identity, reference.outcome_attempt))
                if reference is not None and reference.outcome_attempt is not None
                else None
            )
            validate_tool_message(message.as_fact(), calls, outcome)
            resources[message.message_id] = tuple(
                outcome.value.resource_refs if outcome is not None else message.resource_refs
            )
        return prepared, resources

    def save_version(self, version: HarnessVersion) -> None:
        path = self._version_path(version.session_id, version.version_id)
        if path.exists():
            if self.load_harness(version.session_id, version.version_id) != version:
                raise PersistenceError("immutable Harness checkpoint cannot be overwritten")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        write_model_atomic(path, version)

    def _version_path(self, session_id: str, version_id: str) -> Path:
        return self._record_path(self.directory(session_id) / "harness", version_id)

    @staticmethod
    def _record_path(directory: Path, identity: str) -> Path:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identity) is None:
            raise ValueError("invalid Session record identity")
        return directory / f"{identity}.json"

    @staticmethod
    def _write_suffix(path: Path, offset: int, records: Sequence[OuroModel]) -> None:
        payload = "".join(
            (record.storage_json() if isinstance(record, MessageFact) else record.model_dump_json())
            + "\n"
            for record in records
        ).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "w+b" if offset == 0 and not path.exists() else "r+b"
        with path.open(mode) as stream:
            if stream.seek(0, 2) < offset:
                raise PersistenceError("Session decision offset exceeds journal size")
            stream.seek(offset)
            stream.write(payload)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())

    def _locate_cut(
        self, connection: sqlite3.Connection, request: SessionCommit
    ) -> tuple[MessageTruncation, int, tuple[FileMessageReference, ...]]:
        assert request.before_message_id is not None
        removed: list[FileMessageReference] = []
        first = None
        retained = None
        cut = 0
        session_id = request.before.session_id
        directory = self.directory(session_id)
        for reference in self.messages.index.suffix(connection, request.before_message_id):
            if not removed and reference.message_id != request.before.last_message_id:
                raise SessionCommitConflict("Session message head changed")
            if first is not None:
                retained = self.messages.resolve_record(directory, reference, session_id=session_id)
                break
            removed.append(reference)
            if reference.message_id == request.before_message_id:
                first = self.messages.resolve_record(directory, reference, session_id=session_id)
                cut = reference.offset
        if first is None:
            raise HistoryBoundaryUnavailable("message boundary not found")
        return (
            MessageTruncation(
                first_removed=MessageFact.model_validate(vars(first)),
                last_retained=MessageFact.model_validate(vars(retained)) if retained else None,
                removed_count=len(removed),
            ),
            cut,
            tuple(removed),
        )

    def _validate_appends(
        self, connection: sqlite3.Connection, decision: FileCommitDecision
    ) -> None:
        messages = decision.result.messages
        ids = [message.message_id for message in messages]
        if len(set(ids)) != len(ids):
            raise SessionCommitConflict("message batch contains duplicate identities")
        if self.messages.index.contains_any(connection, ids):
            raise SessionCommitConflict("message identity is already committed")
