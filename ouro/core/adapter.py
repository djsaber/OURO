from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress

from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextView
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata, Provider, ProviderStreamEvent


class RuntimeAdapter:
    def __init__(self, provider: Provider, model: ModelMetadata) -> None:
        self.provider = provider
        self.model = model

    async def stream(
        self,
        context: ContextView,
        *,
        signal: AbortSignal | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        messages = list(context.model_messages)
        if context.system_prompt:
            messages.insert(
                0,
                MessageRecord.text(
                    session_id=messages[-1].session_id if messages else "system",
                    role="system_synthetic",
                    text=context.system_prompt,
                    sequence=-10_000,
                ),
            )
        provider_events = self.provider.stream(
            model=self.model,
            messages=messages,
            tools=[tool.model_dump() for tool in context.tool_definitions],
        )
        async for event in _interruptible(provider_events, signal=signal):
            yield event


async def _interruptible(
    events: AsyncIterator[ProviderStreamEvent],
    *,
    signal: AbortSignal | None,
    poll_interval: float = 0.05,
) -> AsyncIterator[ProviderStreamEvent]:
    if signal is None:
        async for event in events:
            yield event
        return

    try:
        while not signal.is_aborted():
            next_event = asyncio.ensure_future(anext(events))
            abort_wait = asyncio.create_task(_wait_for_abort(signal, poll_interval))
            done, _ = await asyncio.wait(
                {next_event, abort_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if abort_wait in done:
                next_event.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration):
                    await next_event
                break

            abort_wait.cancel()
            with suppress(asyncio.CancelledError):
                await abort_wait

            if signal.is_aborted():
                break

            try:
                yield next_event.result()
            except StopAsyncIteration:
                break
    finally:
        close = getattr(events, "aclose", None)
        if close is not None:
            with suppress(Exception):
                await close()


async def _wait_for_abort(signal: AbortSignal, poll_interval: float) -> None:
    while not signal.is_aborted():
        await asyncio.sleep(poll_interval)
