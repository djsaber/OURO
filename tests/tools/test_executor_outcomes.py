import asyncio

import pytest

from ouro.core.agents import AgentControlRequest
from ouro.core.cancellation import AbortSignal
from ouro.core.interactions import AnswerResolution
from ouro.core.messages import TextContent
from ouro.core.session_controls import SessionControlTransitionRequest
from ouro.core.tools import ToolResultValue
from ouro.tools import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutor,
    ToolInputRequest,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
)


def invoke(stage, handler, *, context=None):
    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(name="Probe", description="Probe", resumable=True),
            handler=handler,
            resume_handler=handler,
        )
    )
    executor = ToolExecutor(registry)
    invocation = ToolInvocation(
        session_id="ses", run_id="run", tool_call_id="call", tool_name="Probe"
    )
    if stage == "resume":
        return executor.resume(
            invocation,
            resolution=AnswerResolution(value="answer"),
            request=ToolInputRequest(data={"question": "continue?"}),
            context=context,
        )
    return executor.execute(invocation, context=context)


@pytest.mark.parametrize("stage", ["execute", "resume"])
@pytest.mark.parametrize("kind", ["result", "input", "control", "invalid", "wrong_identity"])
async def test_execute_and_resume_share_the_outcome_contract(stage, kind):
    calls = []
    value = {
        "result": ToolResult(tool_call_id="call", tool_name="Probe", content="answer"),
        "input": ToolInputRequest(data={"question": "next?"}),
        "control": AgentControlRequest(action="interrupt", delegation_id="agent"),
        "invalid": None,
        "wrong_identity": ToolResult(tool_call_id="other", tool_name="Other", content="wrong"),
    }[kind]

    async def handler(invocation, *args):
        calls.append(invocation.tool_call_id)
        return value

    outcome = await invoke(stage, handler)

    assert calls == ["call"]
    if kind == "input":
        assert outcome.status == "waiting_user"
        assert outcome.input_request == value
    elif kind == "control":
        assert outcome.status == "control_requested"
        assert outcome.control_request == value
    elif kind == "result":
        assert outcome.status == "completed"
        assert outcome.result == value
    else:
        assert outcome.status == "completed"
        assert outcome.result.tool_call_id == "call"
        assert outcome.result.tool_name == "Probe"
        assert outcome.result.is_error
        assert outcome.result.structured["error_type"] == "ToolResultContractError"


@pytest.mark.parametrize("stage", ["execute", "resume"])
async def test_handler_failure_keeps_its_operation_specific_message(stage):
    async def handler(invocation, *args):
        raise ValueError("synthetic failure")

    outcome = await invoke(stage, handler)

    action = "resume failed" if stage == "resume" else "failed"
    assert outcome.status == "completed"
    assert outcome.result.is_error
    assert outcome.result.content == f"Tool Probe {action}: [ValueError] synthetic failure"


@pytest.mark.parametrize("stage", ["execute", "resume"])
async def test_executor_owns_result_and_proposals_after_callback(stage):
    returned = ToolResult(
        tool_call_id="call",
        tool_name="Probe",
        content=[TextContent(text="answer")],
        structured={"items": [1]},
        metadata={"labels": ["original"]},
        session_control_transitions=[
            SessionControlTransitionRequest(
                action="acquire", controller_id="mode", payload={"items": [1]}
            )
        ],
    )

    async def handler(invocation, *args):
        return returned

    outcome = await invoke(stage, handler)
    returned.content[0].text = "changed"
    returned.structured["items"].append(2)
    returned.metadata["labels"].append("changed")
    returned.session_control_transitions[0].payload["items"].append(2)

    observed = outcome.result.observation()
    assert type(observed) is ToolResultValue
    assert observed.text_content == "answer"
    assert observed.structured == {"items": [1]}
    assert observed.metadata == {"labels": ["original"]}
    assert "session_control_transitions" not in observed.model_dump()
    assert outcome.result.session_control_transitions[0].payload == {"items": [1]}


@pytest.mark.parametrize("stage", ["execute", "resume"])
@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_cancellation_settles_the_handler_before_returning(stage, cancel_caller):
    started, finalized = asyncio.Event(), asyncio.Event()
    signal = AbortSignal()

    async def handler(invocation, *args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    execution = asyncio.create_task(
        invoke(stage, handler, context=ToolExecutionContext(signal=signal))
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        if cancel_caller:
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(execution, 5)
        else:
            signal.abort("stop")
            outcome = await asyncio.wait_for(execution, 5)
            assert outcome.status == "unknown_outcome"
            assert outcome.result.structured == {"unknown_outcome": True, "reason": "stop"}
        assert finalized.is_set()
    finally:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
