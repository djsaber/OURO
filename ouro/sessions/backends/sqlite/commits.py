from __future__ import annotations

import sqlite3

from ouro.core.compaction import CompactionRecord, retain_compaction_references
from ouro.core.context_snapshots import ModelRequestHeader
from ouro.core.errors import PersistenceError
from ouro.core.harness import HarnessVersion
from ouro.core.message_content import validate_tool_message
from ouro.core.message_frames import MessageTruncation
from ouro.core.messages import MessageFact
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.session_commits import (
    HistoryBoundaryUnavailable,
    SessionCommit,
    SessionCommitConflict,
    SessionCommitResult,
    prepare_session_commit,
)
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord, SessionRecord
from ouro.core.tool_batches import validate_call_states
from ouro.core.tool_facts import ToolCallFacts, ToolOutcomeRecord
from ouro.sessions.backends.sqlite.inputs import read_state, write_input, write_state
from ouro.sessions.backends.sqlite.messages import truncate_messages, write_messages
from ouro.sessions.sqlite_compactions import (
    insert_compaction,
    read_references,
    update_compaction_boundary,
)
from ouro.sessions.sqlite_drafts import publish_draft
from ouro.sessions.sqlite_events import insert_events, latest_sequence
from ouro.sessions.sqlite_facts import load_required, write_call_state, write_fact
from ouro.sessions.sqlite_requests import write_header
from ouro.sessions.sqlite_runs import read_pending_runs, write_run
from ouro.sessions.sqlite_sessions import read_session


class SQLiteSessionCommits:
    """Native Session commits on the adapter's existing transaction connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def load_session_harness(self, session_id: str) -> tuple[SessionRecord, HarnessVersion]:
        session = read_session(self.connection, session_id)
        version = self.load_harness(session_id, session.harness_version)
        return session, version

    def load_harness(self, session_id: str, version_id: str) -> HarnessVersion:
        row = self.connection.execute(
            "SELECT payload FROM harness_versions WHERE session_id = ? AND version_id = ?",
            (session_id, version_id),
        ).fetchone()
        if row is None:
            raise PersistenceError(f"Harness checkpoint not found: {version_id}")
        version = decode_versioned_record_json(HarnessVersion, row[0], label="Harness checkpoint")
        if version.session_id != session_id or version.version_id != version_id:
            raise PersistenceError("Harness checkpoint has a foreign identity")
        return version

    def save_version(self, version: HarnessVersion) -> None:
        row = self.connection.execute(
            "SELECT payload FROM harness_versions WHERE session_id = ? AND version_id = ?",
            (version.session_id, version.version_id),
        ).fetchone()
        if row is not None:
            if self.load_harness(version.session_id, version.version_id) != version:
                raise PersistenceError("immutable Harness checkpoint cannot be overwritten")
            return
        self.connection.execute(
            "INSERT INTO harness_versions(session_id, version_id, payload) VALUES(?, ?, ?)",
            (version.session_id, version.version_id, version.model_dump_json()),
        )

    def commit(self, request: SessionCommit) -> SessionCommitResult:
        session_id = request.before.session_id
        connection = self.connection
        current = read_session(connection, session_id)
        harness = self.load_harness(session_id, current.harness_version) if request.steps else None
        if current.last_commit_id == request.operation_id:
            raise SessionCommitConflict("commit already applied; query the domain result")
        event_sequence = latest_sequence(connection, session_id)
        truncation = None
        restore = None
        compaction_references = None
        updated_compactions: tuple[CompactionRecord, ...] = ()
        removed_compaction_ids: tuple[str, ...] = ()
        cut_row = None
        withdrawn_inputs: tuple[str, ...] = ()
        if request.before_message_id is not None:
            row = connection.execute(
                "SELECT row_id, payload FROM messages WHERE session_id = ? AND message_id = ?",
                (session_id, request.before_message_id),
            ).fetchone()
            if row is None:
                raise HistoryBoundaryUnavailable("message boundary not found")
            cut_row = row[0]
            withdrawn_inputs = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT json_extract(payload, '$.input_ref.input_id') "
                    "FROM messages WHERE session_id = ? AND row_id >= ? "
                    "AND json_type(payload, '$.input_ref.input_id') = 'text'",
                    (session_id, cut_row),
                )
            )
            first = decode_versioned_record_json(MessageFact, row[1], label="history boundary")
            if first.before_harness_version is None:
                raise HistoryBoundaryUnavailable("message has no Harness checkpoint")
            restore = self.load_harness(session_id, first.before_harness_version)
            previous_row = connection.execute(
                "SELECT payload FROM messages WHERE session_id = ? AND row_id < ? "
                "ORDER BY row_id DESC LIMIT 1",
                (session_id, cut_row),
            ).fetchone()
            retained = (
                None
                if previous_row is None
                else decode_versioned_record_json(
                    MessageFact, previous_row[0], label="retained history head"
                )
            )
            removed = {
                row[0]
                for row in connection.execute(
                    "SELECT message_id FROM messages WHERE session_id = ? AND row_id >= ?",
                    (session_id, cut_row),
                )
            }
            truncation = MessageTruncation(
                first_removed=first, last_retained=retained, removed_count=len(removed)
            )
            records = read_references(connection, session_id)
            compaction_references = tuple(
                retain_compaction_references(
                    records,
                    removed,
                    last_retained_message_id=retained.message_id if retained else None,
                )
            )
            retained_ids = {item.compaction_id for item in compaction_references}
            removed_compaction_ids = tuple(
                item.compaction_id for item in records if item.compaction_id not in retained_ids
            )
            previous = {item.compaction_id: item for item in records}
            updated_compactions = tuple(
                decode_versioned_record_json(
                    CompactionRecord,
                    connection.execute(
                        "SELECT payload FROM compactions "
                        "WHERE session_id = ? AND compaction_id = ?",
                        (session_id, item.compaction_id),
                    ).fetchone()[0],
                    label="compaction boundary",
                ).model_copy(
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
            compaction_sources = tuple(
                decode_versioned_record_json(MessageFact, row[0], label="compaction source")
                for identity in request.compaction.referenced_message_ids
                if (
                    row := connection.execute(
                        "SELECT payload FROM messages WHERE session_id = ? AND message_id = ?",
                        (session_id, identity),
                    ).fetchone()
                )
                is not None
            )
        media_source = None
        selected_compaction = None
        if (
            request.compaction is None
            and request.after is not None
            and (selected_id := request.after.context_selection.compaction_id) is not None
            and selected_id != current.context_selection.compaction_id
        ):
            row = connection.execute(
                "SELECT payload FROM compactions WHERE session_id = ? AND compaction_id = ?",
                (session_id, selected_id),
            ).fetchone()
            if row is not None:
                selected_compaction = decode_versioned_record_json(
                    CompactionRecord, row[0], label="selected compaction"
                )
        if (
            request.after is not None
            and (boundary := request.after.context_selection.media_elided_through) is not None
            and boundary != current.context_selection.media_elided_through
        ):
            row = connection.execute(
                "SELECT payload FROM messages WHERE session_id = ? AND message_id = ?",
                (session_id, boundary.message_id),
            ).fetchone()
            if row is not None:
                media_source = decode_versioned_record_json(
                    MessageFact, row[0], label="media source"
                )
        input_ids = dict.fromkeys(
            [record.item.queue_id for record in request.inputs]
            + [change.input_id for change in request.input_changes]
            + list(withdrawn_inputs)
            + (list(current.pending_input_ids) if request.before_message_id is not None else [])
        )
        input_states = tuple(
            state
            for identity in input_ids
            if (state := read_state(connection, session_id, identity)) is not None
        )
        current_request = None
        if (
            request.request_header is not None
            or any(run.status not in ACTIVE_RUN_STATUSES for run in request.runs)
            or any(
                step.message is not None and step.message.role == "assistant"
                for step in request.steps
            )
        ):
            if request.request_header is not None and (
                request.request_manifest is not None
                or request.request_header.purpose == "compaction"
            ):
                row = connection.execute(
                    "SELECT payload FROM request_headers WHERE session_id = ? AND context_id = ?",
                    (session_id, request.request_header.context_id),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT h.payload FROM request_headers h "
                    "JOIN latest_contexts b USING(session_id, context_id) WHERE h.session_id = ?",
                    (session_id,),
                ).fetchone()
            if row is not None:
                current_request = decode_versioned_record_json(
                    ModelRequestHeader, row[0], label="request header"
                )
        tool_facts, message_resources = self._prepare_tool_facts(request, current)
        decision = prepare_session_commit(
            request,
            current,
            harness,
            event_sequence=event_sequence,
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
            pending_runs=(
                tuple(read_pending_runs(connection, session_id, current.history_epoch))
                if truncation is not None
                else ()
            ),
        )
        result = decision.result
        if decision.clear_response_draft:
            # The formal response can reuse its Draft pages within this same
            # transaction. Readers keep the old snapshot until publication.
            connection.execute("DELETE FROM response_streams WHERE session_id = ?", (session_id,))
        for fact in decision.tool_facts.records:
            write_fact(connection, fact)
        for call_state in decision.call_states:
            write_call_state(connection, call_state)
        for version in decision.versions:
            self.save_version(version)
        for record in decision.inputs:
            write_input(connection, record)
        for state in decision.input_states:
            write_state(connection, state)
        if cut_row is not None:
            truncate_messages(connection, session_id, cut_row)
        write_messages(connection, result.messages, resource_refs=message_resources)
        for run in decision.runs:
            result.session.run_count += int(
                write_run(connection, run, history_epoch=result.session.history_epoch)
            )
        if result.truncation is not None:
            connection.execute(
                "UPDATE runs SET needs_recovery = 0 "
                "WHERE session_id = ? AND history_epoch = ? AND needs_recovery = 1",
                (session_id, result.session.history_epoch - 1),
            )
        for identity in decision.removed_compaction_ids:
            connection.execute(
                "DELETE FROM compactions WHERE session_id = ? AND compaction_id = ?",
                (session_id, identity),
            )
        for item in decision.updated_compactions:
            update_compaction_boundary(connection, item)
        if decision.compaction is not None:
            insert_compaction(connection, decision.compaction)
        if decision.invalidate_context:
            connection.execute("DELETE FROM latest_contexts WHERE session_id = ?", (session_id,))
        if request.request_manifest is not None:
            manifest = request.request_manifest
            connection.execute(
                "INSERT INTO latest_contexts(session_id, context_id, payload) VALUES(?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "context_id = excluded.context_id, payload = excluded.payload",
                (session_id, manifest.context_id, manifest.model_dump_json()),
            )
        if decision.request_header is not None:
            write_header(connection, decision.request_header)
        if decision.request_header is not None:
            publish_draft(connection, decision.request_header)
        connection.execute(
            "UPDATE sessions SET payload = ? WHERE session_id = ?",
            (result.session.model_dump_json(), session_id),
        )
        insert_events(connection, result.events)
        return result

    def commit_outcome(self, outcome: ToolOutcomeRecord) -> ToolOutcomeRecord:
        outcome = ToolOutcomeRecord.model_validate(outcome.model_dump())
        proposed = ToolCallFacts(outcomes=(outcome,))
        prepared = proposed.prepare(
            session_id=outcome.session_id,
            history_epoch=0,
            active_runs={},
            existing=load_required(self.connection, outcome.session_id, proposed),
        )
        if prepared.outcomes:
            write_fact(self.connection, outcome)
        return outcome

    def _prepare_tool_facts(
        self, request: SessionCommit, current: SessionRecord
    ) -> tuple[ToolCallFacts, dict[str, tuple[str, ...]]]:
        proposed = request.tool_facts
        session_id = current.session_id
        connection = self.connection
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
                row = connection.execute(
                    "SELECT payload FROM runs WHERE session_id = ? AND run_id = ?",
                    (session_id, run_id),
                ).fetchone()
                if row is None:
                    continue
                run = decode_versioned_record_json(RunRecord, row[0], label=f"Call Run {run_id}")
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
