from __future__ import annotations

import pytest

from ouro.core.tool_batch_transitions import tool_batch_suspension
from ouro.core.tool_batches import ToolBatchCall, ToolCallBatchRecord, ToolCallStatus
from ouro.core.tools import ToolCallRequest


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
