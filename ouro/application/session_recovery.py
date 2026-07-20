from __future__ import annotations

from typing import Any

from ouro.core.base import utc_now
from ouro.core.errors import PersistenceError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.state import (
    ACTIVE_RUN_STATUSES,
    RunRecord,
    SessionRecord,
    session_status_for_run,
)
from ouro.sessions import RuntimeSessionSnapshot, SessionSnapshot, SessionStore

_INCOMPLETE_RUN_STATUSES = {"queued", "running"}


class SessionRecoveryService:
    """Apply interrupted-run recovery as an explicit application command."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def recover(self, session_id: str) -> SessionSnapshot:
        with self.store.session_mutation(session_id):
            snapshot = self.store.load_session(session_id)
            recovered, changed = self._recover_snapshot(snapshot)
        if changed:
            self.store.refresh_catalog(
                session_id,
                RuntimeSessionSnapshot(
                    session=recovered.session,
                    messages=recovered.messages,
                    runs=recovered.runs,
                    compactions=recovered.compactions,
                ),
            )
        return recovered

    def recover_runtime(self, session_id: str) -> RuntimeSessionSnapshot:
        with self.store.session_mutation(session_id):
            runtime = self.store.load_runtime_session(session_id)
            needs_event_journal = not self.store.is_session_locked(session_id) and any(
                run.status in _INCOMPLETE_RUN_STATUSES for run in runtime.runs
            )
            snapshot = (
                self.store.load_session(session_id)
                if needs_event_journal
                else SessionSnapshot(**runtime.model_dump(), events=[])
            )
            recovered, changed = self._recover_snapshot(snapshot)
        recovered_runtime = RuntimeSessionSnapshot(
            session=recovered.session,
            messages=recovered.messages,
            runs=recovered.runs,
            compactions=recovered.compactions,
        )
        if changed:
            self.store.refresh_catalog(session_id, recovered_runtime)
        return recovered_runtime

    def _recover_snapshot(self, snapshot: SessionSnapshot) -> tuple[SessionSnapshot, bool]:
        session = snapshot.session
        messages = list(snapshot.messages)
        events = list(snapshot.events)
        runs = list(snapshot.runs)
        run_is_live = self.store.is_session_locked(session.session_id)
        recovered_runs: list[RunRecord] = []
        recovered_incomplete = False

        for run in runs:
            interaction_error = _interaction_state_error(run)
            if not run_is_live and (
                run.status in _INCOMPLETE_RUN_STATUSES or interaction_error is not None
            ):
                metadata = dict(run.metadata)
                metadata.pop("checkpoint", None)
                partial_message = (
                    self._append_recovered_partial_message(
                        session=session,
                        run=run,
                        messages=messages,
                        events=events,
                    )
                    if run.status in _INCOMPLETE_RUN_STATUSES
                    else None
                )
                if partial_message is not None:
                    metadata["recovered_partial_assistant_message_id"] = partial_message.message_id
                metadata["recovered_from_status"] = run.status
                if interaction_error is not None:
                    metadata["recovery_reason"] = "invalid_interaction_state"
                active_batch = run.active_tool_batch
                if active_batch is not None and (
                    interaction_error is not None
                    or active_batch.phase in {"executing", "committing_results"}
                ):
                    recovered_calls = [
                        call.model_copy(update={"status": "unknown_outcome"})
                        if call.status in {"running", "resolving_input"}
                        else call
                        for call in active_batch.calls
                    ]
                    active_batch = active_batch.model_copy(
                        update={
                            "phase": "recoverable",
                            "calls": recovered_calls,
                            "updated_at": utc_now(),
                        }
                    )
                recovered = run.model_copy(
                    update={
                        "status": "recoverable",
                        "ended_at": utc_now(),
                        "error": interaction_error or "Run was interrupted before completion.",
                        "abort_requested": False,
                        "active_tool_batch": active_batch,
                        "metadata": metadata,
                    }
                )
                self.store.save_run(recovered)
                recovery_event = self._append_recovery_event(
                    session_id=session.session_id,
                    run_id=recovered.run_id,
                    previous_status=run.status,
                    reason=(
                        "invalid_interaction_state"
                        if interaction_error is not None
                        else "process_restarted"
                    ),
                )
                events.append(recovery_event)
                recovered_runs.append(recovered)
                recovered_incomplete = True
            else:
                recovered_runs.append(run)

        reconciled_session, projection_changed = _reconcile_session_projection(
            session,
            messages,
            recovered_runs,
        )
        if recovered_incomplete or projection_changed:
            metadata = dict(session.metadata)
            if recovered_incomplete:
                metadata["recovered_incomplete_runs"] = True
            if projection_changed:
                metadata["session_state_reconciled"] = True
            session = reconciled_session.model_copy(
                update={
                    "updated_at": utc_now(),
                    "last_activity_at": utc_now(),
                    "metadata": metadata,
                }
            )
            self.store.save_session(session)

        return (
            SessionSnapshot(
                session=session,
                messages=messages,
                events=events,
                runs=recovered_runs,
                compactions=snapshot.compactions,
            ),
            recovered_incomplete or projection_changed,
        )

    def _append_recovered_partial_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        messages: list[MessageRecord],
        events: list[EventRecord],
    ) -> MessageRecord | None:
        run_events = sorted(
            (event for event in events if event.run_id == run.run_id),
            key=lambda event: (event.sequence, event.created_at, event.event_id),
        )
        start_indexes = [
            index
            for index, event in enumerate(run_events)
            if event.event_type == "model_request_started"
        ]
        if not start_indexes:
            return None
        start_event = run_events[start_indexes[-1]]
        turn_id = start_event.payload.get("turn_id")
        streamed_events = run_events[start_indexes[-1] + 1 :]
        if any(
            event.event_type
            in {
                "tool_call_requested",
                "approval_requested",
                "ask_user_requested",
                "tool_call_completed",
            }
            for event in streamed_events
        ):
            return None
        durable_run_messages = [message for message in messages if message.run_id == run.run_id]
        if durable_run_messages and (
            durable_run_messages[-1].role in {"assistant", "tool_result"}
            and durable_run_messages[-1].created_at >= start_event.created_at
        ):
            return None

        text = "".join(
            value
            for event in streamed_events
            if event.event_type == "model_text_delta"
            and isinstance((value := event.payload.get("text")), str)
        )
        reasoning = "".join(
            value
            for event in streamed_events
            if event.event_type == "model_reasoning_delta"
            and isinstance((value := event.payload.get("reasoning_content")), str)
        )
        if not text and not reasoning:
            return None
        metadata: dict[str, Any] = {
            "incomplete": True,
            "recovered_from_event_journal": True,
            "run_id": run.run_id,
        }
        message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="assistant",
            text=text,
            sequence=len(messages) + 1,
            turn_id=turn_id if isinstance(turn_id, str) else None,
        ).model_copy(update={"metadata": metadata, "reasoning_content": reasoning})
        self.store.append_message(message)
        messages.append(message)
        return message

    def _append_recovery_event(
        self,
        *,
        session_id: str,
        run_id: str,
        previous_status: str,
        reason: str,
    ) -> EventRecord:
        return self.store.append_ordered_event(
            EventRecord(
                session_id=session_id,
                run_id=run_id,
                event_type="run_status_changed",
                payload={
                    "from_status": previous_status,
                    "to_status": "recoverable",
                    "reason": reason,
                },
            )
        )


def _reconcile_session_projection(
    session: SessionRecord,
    messages: list[MessageRecord],
    runs: list[RunRecord],
) -> tuple[SessionRecord, bool]:
    active_runs = [run for run in runs if run.status in ACTIVE_RUN_STATUSES]
    if len(active_runs) > 1:
        active_ids = ", ".join(run.run_id for run in active_runs)
        raise PersistenceError(
            f"session {session.session_id} has multiple active runs: {active_ids}"
        )

    latest_run = runs[-1] if runs else None
    active_run = active_runs[0] if active_runs else None
    if active_run is not None and active_run is not latest_run:
        raise PersistenceError(
            f"session {session.session_id} has a non-latest active run: {active_run.run_id}"
        )
    if active_run is not None:
        expected_status = session_status_for_run(active_run.status)
        expected_active_run_id = active_run.run_id
    else:
        expected_status = (
            "failed"
            if latest_run and latest_run.status == "failed" and latest_run.kind == "agent"
            else "idle"
        )
        expected_active_run_id = None

    expected_last_run_id = latest_run.run_id if latest_run is not None else None
    expected_last_message_id = messages[-1].message_id if messages else None
    changes: dict[str, object] = {}
    if session.status != expected_status:
        changes["status"] = expected_status
    if session.active_run_id != expected_active_run_id:
        changes["active_run_id"] = expected_active_run_id
    if session.last_run_id != expected_last_run_id:
        changes["last_run_id"] = expected_last_run_id
    if session.last_message_id != expected_last_message_id:
        changes["last_message_id"] = expected_last_message_id
    if session.message_count != len(messages):
        changes["message_count"] = len(messages)
    if not changes:
        return session, False
    return session.model_copy(update=changes), True


def _interaction_state_error(run: RunRecord) -> str | None:
    if run.status == "waiting_approval":
        batch = run.active_tool_batch
        if batch is None or not batch.pending_approvals:
            return "Run was waiting for approval without a pending approval."
        approval_ids = {approval.approval_id for approval in batch.pending_approvals}
        if run.pending_approval_id not in approval_ids:
            return "Run pending approval does not match its active tool batch."
    if run.status == "waiting_user":
        batch = run.active_tool_batch
        if batch is None or not any(call.status == "waiting_user" for call in batch.calls):
            return "Run was waiting for user input without a waiting tool call."
    return None
