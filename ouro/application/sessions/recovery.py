from __future__ import annotations

from ouro.application.runs.tool_batches import load_batch_states
from ouro.application.usage import UsageService
from ouro.core.base import utc_now
from ouro.core.errors import PersistenceError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.run_transitions import plan_run_transition, session_state_for_run
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord, SessionRecord
from ouro.core.tool_batches import ToolCallState
from ouro.sessions import RuntimeSessionSnapshot, SessionSnapshot, SessionStore

_INCOMPLETE_RUN_STATUSES = {"queued", "running"}


class SessionRecoveryService:
    """Apply interrupted-run recovery as an explicit application command."""

    def __init__(self, store: SessionStore, *, usage_service: UsageService | None = None) -> None:
        self.store = store
        self.usage = usage_service

    def settle_request(self, session_id: str) -> None:
        """Point-settle requests owned by unfinished work and the latest terminal Run."""
        with self.store.session_mutation(session_id):
            if self.store.is_session_locked(session_id):
                return
            session = self.store.load_session_record(session_id)
            runs = self.store.load_pending_runs(session_id)
            if session.last_run_id is not None and not any(
                run.run_id == session.last_run_id for run in runs
            ):
                runs.append(self.store.load_run(session_id, session.last_run_id))
        for run in runs:
            self._settle_run_request(run)

    def _settle_run_request(self, run: RunRecord) -> None:
        session_id = run.session_id
        if run.current_request_id is None:
            return
        with self.store.session_mutation(session_id):
            if self.store.is_session_locked(session_id):
                return
            request = self.store.load_request_header(session_id, run.current_request_id)
            if request is None:
                raise PersistenceError("Run references a missing model request")
            if request.run_id != run.run_id:
                raise PersistenceError("Run references another Run's model request")
            if request.usage_entry_id is not None:
                return
            settled = request.provider_done_at is not None or request.interrupted_at is not None
            if settled and self.usage is None:
                return
            if self.store.load_session_record(session_id).history_epoch != run.history_epoch:
                return
        # The ledger and Session have separate transactions. Recheck ownership
        # after the point lookup; recovery must not close a newly dispatched request.
        entry = self.usage.request_usage(request) if self.usage is not None else None
        if settled and entry is None:
            return
        with self.store.session_mutation(session_id):
            if self.store.is_session_locked(session_id):
                return
            current_run = self.store.load_run(session_id, run.run_id)
            if current_run.current_request_id != request.context_id:
                return
            if self.store.load_request_header(session_id, request.context_id) != request:
                return
            if self.store.load_session_record(session_id).history_epoch != run.history_epoch:
                return
            self.store.settle_request_header(
                session_id,
                request.context_id,
                settled_at=utc_now(),
                provider_done=request.provider_done_at is not None,
                usage_entry_id=entry.entry_id if entry is not None else None,
            )

    def recover(self, session_id: str) -> SessionSnapshot:
        self.settle_request(session_id)
        with self.store.session_mutation(session_id):
            changed = self._recover_runs(self.store.load_runtime_session(session_id))
            recovered = self.store.load_session(session_id)
        if changed:
            self.store.refresh_catalog(session_id)
        return recovered

    def recover_runtime(self, session_id: str) -> RuntimeSessionSnapshot:
        self.settle_request(session_id)
        with self.store.session_mutation(session_id):
            recovered_runtime = self.store.load_runtime_session(session_id)
            changed = self._recover_runs(recovered_runtime)
            if changed:
                recovered_runtime = self.store.load_runtime_session(session_id)
        if changed:
            self.store.refresh_catalog(session_id)
        return recovered_runtime

    def _recover_runs(self, snapshot: RuntimeSessionSnapshot) -> bool:
        session, runs = snapshot.session, snapshot.runs
        run_is_live = self.store.is_session_locked(session.session_id)
        recovered_runs: list[RunRecord] = []
        changed_runs: list[RunRecord] = []
        changed_calls: list[ToolCallState] = []
        steps: list[SessionCommitStep] = []
        recovery_events: list[EventRecord] = []

        for run in runs:
            if (
                run_is_live
                or run.history_epoch != session.history_epoch
                or run.status not in ACTIVE_RUN_STATUSES
            ):
                recovered_runs.append(run)
                continue
            states = load_batch_states(self.store, run)
            interaction_error = _interaction_state_error(run, states)
            if run.status in _INCOMPLETE_RUN_STATUSES or interaction_error is not None:
                metadata = dict(run.metadata)
                partial_message = (
                    self._recovered_partial_message(
                        session=session,
                        run=run,
                    )
                    if run.status in _INCOMPLETE_RUN_STATUSES
                    else None
                )
                if partial_message is not None:
                    metadata["recovered_partial_assistant_message_id"] = partial_message.message_id
                    steps.append(SessionCommitStep(message=partial_message))
                metadata["recovered_from_status"] = run.status
                if interaction_error is not None:
                    metadata["recovery_reason"] = "invalid_interaction_state"
                now = utc_now()
                active_batch = run.active_tool_batch
                if active_batch is not None and (
                    interaction_error is not None
                    or active_batch.phase in {"executing", "committing_results"}
                ):
                    changed_calls.extend(
                        call.model_copy(update={"status": "unknown_outcome"})
                        for call in states
                        if call.status in {"running", "resolving_input"}
                    )
                    active_batch = active_batch.model_copy(
                        update={
                            "phase": "recoverable",
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
                recovery_events.append(
                    EventRecord(
                        session_id=session.session_id,
                        run_id=recovered.run_id,
                        history_epoch=recovered.history_epoch,
                        event_type="run_status_changed",
                        payload={
                            "from_status": run.status,
                            "to_status": "recoverable",
                            "reason": (
                                "invalid_interaction_state"
                                if interaction_error is not None
                                else "process_restarted"
                            ),
                        },
                    )
                )
                recovered_runs.append(recovered)
                changed_runs.append(recovered)
            else:
                recovered_runs.append(run)

        reconciled_session, projection_changed = _reconcile_session_projection(
            session,
            recovered_runs,
        )
        if changed_runs or projection_changed:
            metadata = dict(session.metadata)
            if changed_runs:
                metadata["recovered_incomplete_runs"] = True
            if projection_changed:
                metadata["session_state_reconciled"] = True
            recovered_session = reconciled_session.model_copy(
                update={
                    "updated_at": utc_now(),
                    "last_activity_at": utc_now(),
                    "metadata": metadata,
                }
            )
            self.store.commit_session(
                SessionCommit(
                    before=session,
                    after=recovered_session,
                    steps=tuple(steps),
                    runs=tuple(changed_runs),
                    call_states=tuple(changed_calls),
                    events=tuple(recovery_events),
                )
            )
            return True
        return False

    def _recovered_partial_message(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
    ) -> MessageRecord | None:
        header = self.store.load_latest_context_header(session.session_id)
        if (
            header is None
            or header.run_id != run.run_id
            or header.context_id != run.current_request_id
            or header.response_closed_at is not None
        ):
            return None
        draft = self.store.load_response_draft(session.session_id, header.context_id)
        if draft is None or not draft.chunks:
            return None
        text = "".join(chunk.content for chunk in draft.chunks if chunk.channel == "text")
        reasoning = "".join(chunk.content for chunk in draft.chunks if chunk.channel == "reasoning")
        return MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="assistant",
            text=text,
            turn_id=header.turn_id,
        ).model_copy(
            update={
                "metadata": {
                    "incomplete": True,
                    "recovered_from_draft": True,
                    "run_id": run.run_id,
                },
                "reasoning_content": reasoning,
            }
        )


def _reconcile_session_projection(
    session: SessionRecord,
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
    changes: dict[str, object] = {}
    if session.status != expected_status:
        changes["status"] = expected_status
    if session.active_run_id != expected_active_run_id:
        changes["active_run_id"] = expected_active_run_id
    if session.last_run_id != expected_last_run_id:
        changes["last_run_id"] = expected_last_run_id
    if not changes:
        return session, False
    return session.model_copy(update=changes), True


def _interaction_state_error(run: RunRecord, calls: tuple[ToolCallState, ...]) -> str | None:
    if run.status == "waiting_approval":
        pending = [
            call.approval
            for call in calls
            if call.status == "awaiting_approval" and call.approval is not None
        ]
        if not pending:
            return "Run was waiting for approval without a pending approval."
        approval_ids = {approval.approval_id for approval in pending}
        if run.pending_approval_id not in approval_ids:
            return "Run pending approval does not match its active tool batch."
    if run.status == "waiting_user":
        if not any(call.status == "waiting_user" for call in calls):
            return "Run was waiting for user input without a waiting tool call."
    if run.status == "waiting_child":
        if not any(
            call.status == "waiting_child"
            and call.agent_wait is not None
            and call.agent_wait.state == "pending"
            for call in calls
        ):
            return "Run was waiting for a child without a pending Agent wait."
    return None
