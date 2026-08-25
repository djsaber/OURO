from __future__ import annotations

import asyncio
import threading

import pytest

from ouro.core.cancellation import AbortSignal
from ouro.core.tools import ToolInvocation
from ouro.plugins.api import adapt_sdk_contributions
from ouro.sdk.plugins import v1 as sdk
from ouro.tools import ToolExecutionContext, ToolExecutor, ToolRegistry


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
        sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="blocking",
                    exposed_name="BlockingPluginTool",
                    description="Blocks synchronously.",
                    handler=blocking_handler,
                )
            ]
        )
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
        sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="cooperative",
                    exposed_name="CooperativePluginTool",
                    description="Checks the Host cancellation context.",
                    handler=handler,
                    permission_mode="auto",
                )
            ]
        )
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
