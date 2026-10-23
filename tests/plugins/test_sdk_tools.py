from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import pytest

from ouro.core.cancellation import AbortSignal
from ouro.core.content import ImageContent, TextContent
from ouro.core.interactions import AnswerResolution
from ouro.core.tools import ToolInputRequest, ToolInvocation
from ouro.plugins.inprocess.contributions import adapt_sdk_contributions
from ouro.sdk.plugins import v5 as sdk
from ouro.tools import ToolExecutionContext, ToolExecutor, ToolRegistry


async def test_sdk_rich_content_is_preserved_for_execution_and_resume():
    output = sdk.ToolOutput(
        content=(sdk.ToolTextContent("rendered"), sdk.ToolImageContent("https://image.test/one"))
    )
    contribution = sdk.ToolContribution(
        id="render",
        exposed_name="Render",
        description="render",
        handler=lambda call: output,
        resume_handler=lambda call: output,
    )
    tool = adapt_sdk_contributions(
        sdk.ContributionSet(tools=[contribution]), owner_id="example"
    ).tools[0]
    invocation = ToolInvocation(tool_name="Render", session_id="s", run_id="r")
    context = ToolExecutionContext()
    first = await tool.execute(invocation, context=context)
    resumed = await tool.resume(
        invocation,
        AnswerResolution(value="yes"),
        request=ToolInputRequest(data={}),
        context=context,
    )
    for result in (first, resumed):
        assert result.content_blocks == [
            TextContent(text="rendered"),
            ImageContent(uri="https://image.test/one"),
        ]
        assert result.text_content == "rendered"


async def test_sdk_callback_forms_are_bound_before_execution_and_resume(tmp_path):
    seen = []

    async def execute(call):
        seen.append(call.tool_call_id)
        return sdk.ToolInputRequest(data={"type": "question"})

    def resume(call, *, context):
        seen.append(context.workspace)
        return sdk.ToolOutput(content=call.resolution.value)

    tool = adapt_sdk_contributions(
        sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="question",
                    exposed_name="Question",
                    description="Question",
                    handler=execute,
                    resume_handler=resume,
                )
            ]
        ),
        owner_id="example.plugin",
    ).tools[0]
    invocation = ToolInvocation(tool_name="Question", session_id="session", run_id="run")
    context = ToolExecutionContext(workspace=tmp_path)
    with patch("ouro.plugins.harness_calls.signature", side_effect=AssertionError("inspected")):
        request = await tool.execute(invocation, context=context)
        result = await tool.resume(
            invocation,
            AnswerResolution(value="answered"),
            request=request,
            context=context,
        )
    assert result.content == "answered"
    assert seen == [invocation.tool_call_id, str(tmp_path)]


def test_sdk_callback_with_missing_required_argument_fails_during_adaptation():
    def invalid(call, context, unexpected):
        pytest.fail("invalid callback must not run")

    with pytest.raises(TypeError, match="unexpected"):
        adapt_sdk_contributions(
            sdk.ContributionSet(
                tools=[
                    sdk.ToolContribution(
                        id="invalid",
                        exposed_name="Invalid",
                        description="Invalid",
                        handler=invalid,
                    )
                ]
            ),
            owner_id="example.plugin",
        )


@pytest.mark.asyncio
async def test_sync_sdk_tool_handler_does_not_block_the_event_loop() -> None:
    entered = threading.Event()
    release = threading.Event()
    handler_threads: list[int] = []

    def blocking_handler(call: sdk.ToolCall) -> sdk.ToolOutput:
        handler_threads.append(threading.get_ident())
        entered.set()
        release.wait()
        return sdk.ToolOutput(content=f"completed {call.tool_name}")

    contributions = adapt_sdk_contributions(
        owner_id="example.plugin",
        contributions=sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="blocking",
                    exposed_name="BlockingPluginTool",
                    description="Blocks synchronously.",
                    handler=blocking_handler,
                )
            ]
        ),
    )
    tool = contributions.tools[0]
    watchdog = threading.Timer(1, release.set)
    watchdog.start()
    event_loop_thread = threading.get_ident()
    execution = asyncio.create_task(
        tool.execute(
            ToolInvocation(
                session_id="ses_1",
                run_id="run_1",
                tool_name="BlockingPluginTool",
            ),
            context=ToolExecutionContext(),
        )
    )

    try:
        handler_entered = await asyncio.to_thread(entered.wait, 2)
        event_loop_remained_responsive = not execution.done()
    finally:
        release.set()
        watchdog.cancel()
    result = await execution

    assert handler_entered is True
    assert event_loop_remained_responsive is True
    assert handler_threads != [event_loop_thread]
    assert result.content == "completed BlockingPluginTool"


@pytest.mark.asyncio
async def test_sync_sdk_tool_uses_host_cancellation_context() -> None:
    entered = threading.Event()
    checkpoint = threading.Event()

    def handler(
        call: sdk.ToolCall,
        context: sdk.ToolExecutionContext,
    ) -> sdk.ToolOutput:
        entered.set()
        while True:
            context.raise_if_aborted()
            checkpoint.wait(timeout=0.01)

    contributions = adapt_sdk_contributions(
        owner_id="example.plugin",
        contributions=sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="cooperative",
                    exposed_name="CooperativePluginTool",
                    description="Checks the Host cancellation context.",
                    handler=handler,
                    permission_mode="auto",
                )
            ]
        ),
    )
    registry = ToolRegistry()
    registry.register(contributions.tools[0])
    signal = AbortSignal()
    execution = asyncio.create_task(
        ToolExecutor(registry).execute(
            ToolInvocation(
                session_id="ses_1",
                run_id="run_1",
                tool_name="CooperativePluginTool",
            ),
            signal=signal,
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)

    signal.abort("cancel SDK tool")
    outcome = await asyncio.wait_for(execution, timeout=1)

    assert outcome.status == "aborted"
    assert outcome.result is not None
    assert outcome.result.structured == {
        "aborted": True,
        "reason": "cancel SDK tool",
    }
