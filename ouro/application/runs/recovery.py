from __future__ import annotations

from typing import TYPE_CHECKING

from ouro.application.inputs.queue import PendingInputs
from ouro.application.runs.tool_batches import load_batch_states, load_tool_batch
from ouro.core.base import utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.session_ports import RunReferenceReader, RuntimeSessionSnapshot, SessionRepository
from ouro.core.state import ACTIVE_RUN_STATUSES, RESUMABLE_RUN_STATUSES
from ouro.core.tool_batches import ToolBatchCall, tool_execution_in_flight
from ouro.core.tools import ToolResult, ToolResultFeedback, ToolResultValue

if TYPE_CHECKING:
    from ouro.application.events import RuntimeEventRecorder
    from ouro.application.runs.lifecycle import RunLifecycle


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
                        ToolResult.model_validate(observed, from_attributes=True)
                    ),
                    "feedback": feedback,
                }
            )
        if call.result is not None:
            return call
        outcome_unknown = call.status == "unknown_outcome" or tool_execution_in_flight(call)
        content = (
            "Tool execution was interrupted and its outcome is unknown."
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


def retained_plugin_releases(
    store: RunReferenceReader | None,
) -> dict[tuple[str, str, str], set[str]]:
    """Scan recovery facts once on the management path, without loading message journals."""
    references: dict[tuple[str, str, str], set[str]] = {}
    if store is None:
        return references

    def retain(kind: str, identity: str, release: str, owner: str) -> None:
        references.setdefault((kind, identity, release), set()).add(owner)

    queue = PendingInputs(store)
    for session in store.list_sessions():
        for driver in queue.bound_drivers(session):
            for implementation in driver.implementations:
                retain(
                    implementation.plugin_class,
                    implementation.plugin_id,
                    implementation.release_id,
                    f"{session.session_id}/queued",
                )
        for activation in session.controls.activations:
            retain(
                activation.plugin_class,
                activation.plugin_id,
                activation.release_id,
                session.session_id,
            )
        for run in store.load_runs(session.session_id):
            resumable_driver = (
                run.driver_checkpoint is not None
                and run.driver_checkpoint.state in {"pending", "ready"}
                and run.active_tool_batch is not None
            )
            retained = run.status in ACTIVE_RUN_STATUSES or run.status in {"paused", "recoverable"}
            owner = f"{session.session_id}/{run.run_id}"
            if retained or resumable_driver:
                for implementation in run.driver.implementations:
                    retain(
                        implementation.plugin_class,
                        implementation.plugin_id,
                        implementation.release_id,
                        owner,
                    )
            if not retained:
                continue
            for call in load_batch_states(store, run):
                invocation = call.prepared_binding
                if invocation is None and call.attempt > 0:
                    started = store.load_tool_call_start(
                        session.session_id, call.identity, call.attempt
                    )
                    invocation = started.binding if started is not None else None
                if (
                    call.status
                    not in {
                        "planned",
                        "ready",
                        "running",
                        "awaiting_approval",
                        "waiting_user",
                        "resolving_input",
                    }
                    or invocation is None
                ):
                    continue
                if (
                    invocation.plugin_class is not None
                    and invocation.plugin_id is not None
                    and invocation.release_id is not None
                ):
                    retain(
                        invocation.plugin_class,
                        invocation.plugin_id,
                        invocation.release_id,
                        owner,
                    )
    return references
