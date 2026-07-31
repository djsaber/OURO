from __future__ import annotations

from dataclasses import dataclass

from ouro.core.state import RunPhase
from ouro.core.tool_batches import (
    ToolBatchCall,
    ToolBatchOutcomeStatus,
    ToolBatchPhase,
    ToolCallBatchRecord,
    ToolCallStatus,
)

_SUSPENSIONS: tuple[
    tuple[ToolCallStatus, ToolBatchOutcomeStatus, RunPhase, ToolBatchPhase], ...
] = (
    ("awaiting_approval", "waiting_approval", "human_interaction", "awaiting_interaction"),
    ("waiting_child", "waiting_child", "subagent_join", "awaiting_child"),
    ("waiting_user", "waiting_user", "human_interaction", "awaiting_interaction"),
)


@dataclass(frozen=True, slots=True)
class ToolBatchSuspension:
    """One durable reason why a tool batch cannot continue its model turn."""

    status: ToolBatchOutcomeStatus
    run_phase: RunPhase
    batch_phase: ToolBatchPhase
    call: ToolBatchCall


def tool_batch_suspension(batch: ToolCallBatchRecord) -> ToolBatchSuspension | None:
    """Return the deterministic suspension that owns the batch, if any.

    Approval is selected first because it gates execution permission. A child
    join owns the batch before ordinary user input once permission is settled.
    Source ordering resolves multiple calls of the same kind.
    """

    ordered = sorted(batch.calls, key=lambda item: item.request.source_index)
    for call_status, status, run_phase, batch_phase in _SUSPENSIONS:
        call = next((item for item in ordered if item.status == call_status), None)
        if call is not None:
            return ToolBatchSuspension(
                status=status,
                run_phase=run_phase,
                batch_phase=batch_phase,
                call=call,
            )
    return None
