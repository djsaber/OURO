from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress

from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextView
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
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        model = self.model
        if max_output_tokens is not None:
            if max_output_tokens < 1 or max_output_tokens > self.model.context_window:
                raise ValueError("max_output_tokens must be between 1 and the context window")
            model = self.model.model_copy(update={"max_output_tokens": max_output_tokens})
        provider_events = self.provider.stream(
            model=model,
            system_prompt=context.system_prompt,
            messages=list(context.model_messages),
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
