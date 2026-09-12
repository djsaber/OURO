from __future__ import annotations

from typing import Any

from ouro.core.base import utc_now
from ouro.core.errors import PersistenceError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.run_transitions import plan_run_transition, session_state_for_run
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord, SessionRecord
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
            # Recovery can create native receipts and checkpoints. Complete
            # maintenance reads return the committed aggregate, not a rebuilt DTO.
            recovered = self.store.load_session(session_id) if changed else snapshot
        if changed:
            self.store.refresh_catalog(session_id)
        return recovered

    def recover_runtime(self, session_id: str) -> RuntimeSessionSnapshot:
        with self.store.session_mutation(session_id):
            runtime = self.store.load_runtime_session(session_id)
            incomplete_run_ids = tuple(
                run.run_id
                for run in runtime.runs
                if run.history_epoch == runtime.session.history_epoch
                and run.status in _INCOMPLETE_RUN_STATUSES
            )
            events = (
                self._load_run_events(session_id, incomplete_run_ids)
                if incomplete_run_ids and not self.store.is_session_locked(session_id)
                else []
            )
            snapshot = SessionSnapshot(**runtime.model_dump(), events=events)
            recovered, changed = self._recover_snapshot(snapshot)
        recovered_runtime = RuntimeSessionSnapshot(
            session=recovered.session,
            messages=recovered.messages,
            runs=recovered.runs,
            compactions=recovered.compactions,
        )
        if changed:
            self.store.refresh_catalog(session_id)
        return recovered_runtime

    def _load_run_events(
        self,
        session_id: str,
        run_ids: tuple[str, ...],
    ) -> list[EventRecord]:
        """Read only the event histories required to recover interrupted Runs."""

        events: list[EventRecord] = []
        for run_id in run_ids:
            cursor = 0
            while True:
                page, next_cursor = self.store.load_events_page(
                    session_id,
                    cursor=cursor,
                    limit=1000,
                    run_id=run_id,
                )
                events.extend(page)
                if next_cursor is None:
                    break
                if next_cursor <= cursor:
                    raise PersistenceError(
                        f"event journal cursor did not advance for session {session_id}"
                    )
                cursor = next_cursor
        return events

    def _recover_snapshot(self, snapshot: SessionSnapshot) -> tuple[SessionSnapshot, bool]:
        session = snapshot.session
        messages = list(snapshot.messages)
        events = list(snapshot.events)
        runs = list(snapshot.runs)
        run_is_live = self.store.is_session_locked(session.session_id)
        recovered_runs: list[RunRecord] = []
        recovered_incomplete = False

        for run in runs:
            if run.history_epoch != session.history_epoch:
                recovered_runs.append(run)
                continue
            interaction_error = _interaction_state_error(run)
            if not run_is_live and (
                run.status in _INCOMPLETE_RUN_STATUSES or interaction_error is not None
            ):
                metadata = dict(run.metadata)
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
                now = utc_now()
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
                            "updated_at": now,
                        }
                    )
                transition = plan_run_transition(
                    session,
                    current_run=run,
                    next_run=run.model_copy(
                        update={
                            "status": "recoverable",
                            "ended_at": now,
                            "error": (
                                interaction_error or "Run was interrupted before completion."
                            ),
                            "abort_requested": False,
                            "active_tool_batch": active_batch,
                            "metadata": metadata,
                        }
                    ),
                    now=now,
                )
                recovered = transition.run
                self.store.save_run(recovered)
                recovery_event = self._append_recovery_event(
                    session_id=session.session_id,
                    run_id=recovered.run_id,
                    history_epoch=recovered.history_epoch,
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
        # A resumed Run shares its audit journal with older attempts. A crash
        # before the next model request must not resurrect their withdrawn text.
        resumed_at = max(
            (
                event.sequence
                for event in run_events
                if event.event_type == "run_status_changed" and event.payload.get("resumed") is True
            ),
            default=0,
        )
        start_indexes = [
            index
            for index, event in enumerate(run_events)
            if event.event_type == "model_request_started" and event.sequence > resumed_at
        ]
        if not start_indexes:
            return None
        start_event = run_events[start_indexes[-1]]
        turn_id = start_event.turn_id
        if turn_id is None:
            raise PersistenceError(
                f"model request event has no turn identity: {start_event.event_id}"
            )
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
        if _model_round_is_durable(
            durable_run_messages,
            turn_id=turn_id,
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
            turn_id=turn_id,
        ).model_copy(update={"metadata": metadata, "reasoning_content": reasoning})
        self.store.append_message(message)
        messages.append(message)
        return message

    def _append_recovery_event(
        self,
        *,
        session_id: str,
        run_id: str,
        history_epoch: int,
        previous_status: str,
        reason: str,
    ) -> EventRecord:
        return self.store.append_ordered_event(
            EventRecord(
                session_id=session_id,
                run_id=run_id,
                history_epoch=history_epoch,
                event_type="run_status_changed",
                payload={
                    "from_status": previous_status,
                    "to_status": "recoverable",
                    "reason": reason,
                },
            )
        )


def _model_round_is_durable(messages: list[MessageRecord], *, turn_id: str) -> bool:
    return any(
        message.turn_id == turn_id and message.role in {"assistant", "tool_result"}
        for message in messages
    )


def _reconcile_session_projection(
    session: SessionRecord,
    messages: list[MessageRecord],
    runs: list[RunRecord],
) -> tuple[SessionRecord, bool]:
    runs = [run for run in runs if run.history_epoch == session.history_epoch]
    active_runs = [run for run in runs if run.status in ACTIVE_RUN_STATUSES]
    if len(active_runs) > 1:
        active_ids = ", ".join(run.run_id for run in active_runs)
        raise PersistenceError(
            f"session {session.session_id} has multiple active runs: {active_ids}"
        )

    active_run = active_runs[0] if active_runs else None
    if active_run is not None and active_run is not runs[-1]:
        later_runs = runs[runs.index(active_run) + 1 :]
        retained_runs = {message.run_id for message in messages}
        if active_run.kind != "agent" or any(
            run.kind != "compaction" and run.run_id in retained_runs for run in later_runs
        ):
            raise PersistenceError(
                f"session {session.session_id} has a non-latest active run: {active_run.run_id}"
            )
    # Creation order is not execution order after resuming a Run following
    # manual compaction. Project the live owner or the most recent settlement.
    latest_run = active_run or max(
        runs, key=lambda run: run.ended_at or run.started_at, default=None
    )
    if active_run is not None:
        expected_status, expected_active_run_id = session_state_for_run(active_run)
    else:
        expected_status, expected_active_run_id = (
            session_state_for_run(latest_run) if latest_run is not None else ("idle", None)
        )

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
    if run.status == "waiting_child":
        batch = run.active_tool_batch
        if batch is None or not any(
            call.status == "waiting_child"
            and call.agent_wait is not None
            and call.agent_wait.state == "pending"
            for call in batch.calls
        ):
            return "Run was waiting for a child without a pending Agent wait."
    return None
