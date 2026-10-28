from __future__ import annotations

from ouro.application.events import RuntimeEventRecorder
from ouro.application.runs.lifecycle import RunLifecycle
from ouro.application.runs.tool_batches import load_tool_batch
from ouro.core.base import utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.state import RESUMABLE_RUN_STATUSES
from ouro.core.tool_batches import ToolBatchCall
from ouro.core.tools import ToolResult, ToolResultFeedback, ToolResultValue
from ouro.sessions import RuntimeSessionSnapshot, SessionRepository


class RunRecoveryService:
    """Close interrupted tool protocol exchanges before a new run starts."""

    def __init__(
        self,
        *,
        store: SessionRepository,
        runs: RunLifecycle,
        events: RuntimeEventRecorder,
    ) -> None:
        self.store = store
        self.runs = runs
        self.events = events

    async def reconcile(
        self,
        snapshot: RuntimeSessionSnapshot,
        *,
        run_ids: frozenset[str] | None = None,
    ) -> RuntimeSessionSnapshot:
        changed = False
        for run in snapshot.runs:
            if run.history_epoch != snapshot.session.history_epoch:
                continue
            if run_ids is not None and run.run_id not in run_ids:
                continue
            if run.status not in RESUMABLE_RUN_STATUSES:
                continue
            batch = await run_blocking(load_tool_batch, self.store, run)
            if batch is None:
                continue
            calls = []
            for call in batch.calls:
                observed = None
                if call.result is not None and (
                    call.result.harness_change is not None
                    or call.result.session_control_transitions
                ):
                    outcome = await run_blocking(
                        self.store.load_tool_outcome,
                        run.session_id,
                        batch.head().identity(run.run_id, call.request.id),
                        call.attempt,
                    )
                    if outcome is not None:
                        observed = outcome.value
                calls.append(self._recover_call(call, observed=observed))
            unpublished = not all(call.result_message_id is not None for call in batch.calls)
            if unpublished:
                _, _, _, _, committed_events = await run_blocking(
                    self.runs.publish_tool_batch,
                    session=snapshot.session,
                    run=run,
                    batch=batch.model_copy(update={"calls": calls}),
                    recovering=True,
                )
                await self.events.publish_committed(committed_events)
                changed = True
        if not changed:
            return snapshot
        return await run_blocking(self.store.load_runtime_session, snapshot.session.session_id)

    @staticmethod
    def _recover_call(
        call: ToolBatchCall, *, observed: ToolResultValue | None = None
    ) -> ToolBatchCall:
        if observed is not None:
            feedback = ToolResultFeedback(
                error_type="ToolBusinessEffectsNotApplied",
                error_message=(
                    "The tool returned the observation above, but its Session state or control "
                    "changes were not published before interruption and have been discarded."
                ),
            )
            return call.model_copy(
                update={
                    "status": "failed",
                    "result": feedback.materialize(
                        ToolResult.model_validate(observed.model_dump())
                    ),
                    "feedback": feedback,
                }
            )
        if call.result is not None:
            return call
        outcome_unknown = call.status in {"running", "resolving_input", "unknown_outcome"}
        content = (
            "Tool execution was interrupted and its outcome is unknown. "
            "Do not repeat side-effecting work without first verifying external state."
            if outcome_unknown
            else "Tool execution was interrupted before a durable result was produced."
        )
        return call.model_copy(
            update={
                "status": "unknown_outcome" if outcome_unknown else "aborted",
                "result": ToolResult(
                    tool_call_id=call.request.id,
                    tool_name=call.request.name,
                    is_error=True,
                    content=content,
                    structured={"recovered_interruption": True, "outcome_unknown": outcome_unknown},
                    ended_at=utc_now(),
                ),
            }
        )
