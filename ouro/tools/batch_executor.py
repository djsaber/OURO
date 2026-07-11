from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ouro.core.cancellation import AbortSignal
from ouro.core.tool_batches import ToolExecutionMode
from ouro.core.tools import ToolExecutionOutcome, ToolInvocation
from ouro.tools.executor import ToolExecutor


class ToolBatchExecutor:
    """Execute prepared invocations with bounded, failure-isolated concurrency."""

    def __init__(self, executor: ToolExecutor) -> None:
        self.executor = executor

    async def execute(
        self,
        invocations: list[ToolInvocation],
        *,
        mode: ToolExecutionMode,
        max_concurrency: int,
        signal: AbortSignal | None = None,
        on_outcome: Callable[[ToolExecutionOutcome], Awaitable[None]] | None = None,
    ) -> list[ToolExecutionOutcome]:
        if mode == "sequential":
            outcomes: list[ToolExecutionOutcome] = []
            for invocation in invocations:
                outcome = await self.executor.execute_prepared(invocation, signal=signal)
                if on_outcome is not None:
                    await on_outcome(outcome)
                outcomes.append(outcome)
            return outcomes

        semaphore = asyncio.Semaphore(max_concurrency)

        async def run(invocation: ToolInvocation) -> ToolExecutionOutcome:
            async with semaphore:
                outcome = await self.executor.execute_prepared(invocation, signal=signal)
                if on_outcome is not None:
                    await on_outcome(outcome)
                return outcome

        return list(await asyncio.gather(*(run(invocation) for invocation in invocations)))
