from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ouro.core.cancellation import AbortSignal
from ouro.core.tool_batches import ToolExecutionMode
from ouro.core.tools import ToolExecutionOutcome, ToolInvocation
from ouro.tools.base import ToolExecutionContext
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
        context: ToolExecutionContext | None = None,
        resolve_outcome: (
            Callable[[ToolExecutionOutcome], Awaitable[ToolExecutionOutcome]] | None
        ) = None,
        pre_execute: (Callable[[ToolInvocation], Awaitable[ToolExecutionOutcome]] | None) = None,
        on_start: Callable[[ToolInvocation], Awaitable[None]] | None = None,
        on_outcome: Callable[[ToolExecutionOutcome], Awaitable[None]] | None = None,
    ) -> list[ToolExecutionOutcome]:
        async def execute_one(invocation: ToolInvocation) -> ToolExecutionOutcome:
            prepared = (
                await pre_execute(invocation)
                if pre_execute is not None
                else ToolExecutionOutcome(status="ready", invocation=invocation)
            )
            if prepared.status == "ready" and (signal is None or not signal.is_aborted()):
                if on_start is not None:
                    await on_start(prepared.invocation)
            outcome = (
                await self.executor.execute_prepared(
                    prepared.invocation,
                    signal=signal,
                    context=context,
                )
                if prepared.status == "ready"
                else prepared
            )
            if resolve_outcome is not None:
                outcome = await resolve_outcome(outcome)
            if on_outcome is not None:
                await on_outcome(outcome)
            return outcome

        if mode == "sequential":
            outcomes: list[ToolExecutionOutcome] = []
            for invocation in invocations:
                if signal is not None and signal.is_aborted():
                    break
                outcomes.append(await execute_one(invocation))
            return outcomes

        semaphore = asyncio.Semaphore(max_concurrency)

        async def run(invocation: ToolInvocation) -> ToolExecutionOutcome | None:
            async with semaphore:
                if signal is not None and signal.is_aborted():
                    return None
                return await execute_one(invocation)

        completed = await asyncio.gather(
            *(run(invocation) for invocation in invocations),
            return_exceptions=True,
        )
        errors = [result for result in completed if isinstance(result, BaseException)]
        if errors:
            raise errors[0]
        return [result for result in completed if isinstance(result, ToolExecutionOutcome)]
