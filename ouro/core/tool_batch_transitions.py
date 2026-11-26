from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from ouro.core.approvals import ApprovalResolvedBy
from ouro.core.base import utc_now
from ouro.core.state import RunPhase
from ouro.core.tool_batches import (
    ToolBatchCall,
    ToolBatchOutcomeStatus,
    ToolBatchPhase,
    ToolCallBatchRecord,
    ToolCallStatus,
)
from ouro.core.tools import ToolResult

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


def primary_tool_wait(statuses: Iterable[ToolCallStatus]) -> ToolBatchSuspension | None:
    """Project the primary Run wait from its independently addressable Calls."""
    pending = set(statuses)
    for call_status, status, run_phase, batch_phase in _SUSPENSIONS:
        if call_status in pending:
            return ToolBatchSuspension(status, run_phase, batch_phase)
    return None


def tool_batch_suspension(batch: ToolCallBatchRecord) -> ToolBatchSuspension | None:
    if any(call.status in {"ready", "running", "resolving_input"} for call in batch.calls):
        return None
    return primary_tool_wait(call.status for call in batch.calls)


def resolve_tool_approval(
    batch: ToolCallBatchRecord,
    *,
    approval_id: str,
    approved: bool,
    reason: str,
    arguments_override: dict[str, object] | None,
    resolved_by: ApprovalResolvedBy = "user",
    resolver_id: str | None = None,
    resolved_at: datetime | None = None,
) -> ToolCallBatchRecord:
    """Resolve one durable approval without consulting the tool execution environment.

    Approval is a Host-owned interaction fact. Tool lookup, argument validation, policy
    evaluation, and execution-contract checks belong to the later execution boundary.
    """

    timestamp = resolved_at or utc_now()
    resolved_calls: list[ToolBatchCall] = []
    found = False
    for call in batch.calls:
        approval = call.approval
        if approval is None or approval.approval_id != approval_id:
            resolved_calls.append(call)
            continue
        found = True
        if call.status != "awaiting_approval" or call.invocation is None:
            raise ValueError(f"approval is not pending: {approval_id}")

        if approved:
            invocation = (
                call.invocation.model_copy(update={"arguments": arguments_override})
                if arguments_override is not None
                else call.invocation
            )
            resolved_calls.append(
                call.model_copy(
                    update={
                        "status": "ready",
                        "invocation": invocation,
                        "approval": approval.model_copy(
                            update={
                                "status": (
                                    "modified" if arguments_override is not None else "approved"
                                ),
                                "resolved_at": timestamp,
                                "resolved_by": resolved_by,
                                "resolver_id": resolver_id,
                                "modified_arguments": arguments_override,
                            }
                        ),
                    }
                )
            )
            continue

        result = ToolResult(
            tool_call_id=call.invocation.tool_call_id,
            tool_name=call.invocation.tool_name,
            is_error=True,
            content=reason,
            structured={"rejected": True, "reason": reason},
            ended_at=timestamp,
        )
        resolved_calls.append(
            call.model_copy(
                update={
                    "status": "rejected",
                    "approval": approval.model_copy(
                        update={
                            "status": "rejected",
                            "resolved_at": timestamp,
                            "resolved_by": resolved_by,
                            "resolver_id": resolver_id,
                            "reason": reason,
                        }
                    ),
                    "result": result,
                }
            )
        )

    if not found:
        raise ValueError(f"unknown approval: {approval_id}")
    return batch.model_copy(update={"calls": resolved_calls, "updated_at": timestamp})
