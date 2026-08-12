from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ouro.core.approvals import ApprovalRecord
from ouro.core.tool_batch_transitions import resolve_tool_approval, tool_batch_suspension
from ouro.core.tool_batches import ToolBatchCall, ToolCallBatchRecord, ToolCallStatus
from ouro.core.tools import ToolCallRequest, ToolInvocation


def _call(status: ToolCallStatus, source_index: int) -> ToolBatchCall:
    return ToolBatchCall(
        request=ToolCallRequest(
            id=f"call_{source_index}",
            name="Test",
            source_index=source_index,
        ),
        status=status,
    )


@pytest.mark.parametrize(
    ("call_status", "run_status", "run_phase", "batch_phase"),
    [
        (
            "awaiting_approval",
            "waiting_approval",
            "human_interaction",
            "awaiting_interaction",
        ),
        ("waiting_child", "waiting_child", "subagent_join", "awaiting_child"),
        ("waiting_user", "waiting_user", "human_interaction", "awaiting_interaction"),
    ],
)
def test_tool_batch_suspension_projects_durable_run_and_batch_phase(
    call_status: ToolCallStatus,
    run_status: str,
    run_phase: str,
    batch_phase: str,
) -> None:
    suspension = tool_batch_suspension(
        ToolCallBatchRecord(
            turn_id="turn_1",
            assistant_message_id="msg_1",
            calls=[_call(call_status, 0)],
        )
    )

    assert suspension is not None
    assert suspension.status == run_status
    assert suspension.run_phase == run_phase
    assert suspension.batch_phase == batch_phase


def test_tool_batch_suspension_has_stable_priority_and_source_order() -> None:
    batch = ToolCallBatchRecord(
        turn_id="turn_1",
        assistant_message_id="msg_1",
        calls=[
            _call("waiting_user", 0),
            _call("waiting_child", 1),
            _call("awaiting_approval", 3),
            _call("awaiting_approval", 2),
        ],
    )

    suspension = tool_batch_suspension(batch)

    assert suspension is not None
    assert suspension.status == "waiting_approval"
    assert suspension.call.request.id == "call_2"


def test_completed_tool_batch_has_no_suspension() -> None:
    batch = ToolCallBatchRecord(
        turn_id="turn_1",
        assistant_message_id="msg_1",
        calls=[_call("completed", 0), _call("failed", 1)],
    )

    assert tool_batch_suspension(batch) is None


@pytest.mark.parametrize("approved", [True, False])
def test_tool_approval_resolution_is_independent_of_tool_registry(approved: bool) -> None:
    resolved_at = datetime(2026, 8, 12, tzinfo=UTC)
    invocation = ToolInvocation(
        session_id="ses_1",
        run_id="run_1",
        tool_call_id="call_1",
        tool_name="PluginTool",
        arguments={"value": "original"},
    )
    approval = ApprovalRecord(
        approval_id="apv_1",
        session_id=invocation.session_id,
        run_id=invocation.run_id,
        tool_call_id=invocation.tool_call_id,
    )
    batch = ToolCallBatchRecord(
        turn_id="turn_1",
        assistant_message_id="msg_1",
        phase="awaiting_interaction",
        calls=[
            ToolBatchCall(
                request=ToolCallRequest(id="call_1", name="PluginTool"),
                status="awaiting_approval",
                invocation=invocation,
                approval=approval,
            )
        ],
    )

    resolved = resolve_tool_approval(
        batch,
        approval_id=approval.approval_id,
        approved=approved,
        reason="not allowed",
        arguments_override={"value": "modified"} if approved else None,
        resolved_at=resolved_at,
    )

    call = resolved.calls[0]
    assert resolved.phase == "ready"
    assert call.approval is not None
    assert call.approval.resolved_at == resolved_at
    if approved:
        assert call.status == "ready"
        assert call.approval.status == "modified"
        assert call.invocation is not None
        assert call.invocation.arguments == {"value": "modified"}
        assert call.result is None
    else:
        assert call.status == "rejected"
        assert call.approval.status == "rejected"
        assert call.result is not None
        assert call.result.structured == {"rejected": True, "reason": "not allowed"}
