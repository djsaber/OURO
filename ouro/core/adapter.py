from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass

from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextView
from ouro.core.provider import ModelMetadata, Provider, ProviderStreamEvent
from ouro.core.reasoning import ReasoningRequest

_STREAM_BUFFER_CAPACITY = 256
_STREAM_COALESCE_MAX_CHARS = 16 * 1024


@dataclass(frozen=True)
class _StreamEnd:
    error: Exception | None = None


_BufferedStreamItem = ProviderStreamEvent | _StreamEnd


class RuntimeAdapter:
    def __init__(self, provider: Provider, model: ModelMetadata) -> None:
        self.provider = provider
        self.model = model

    def with_reasoning(self, request: ReasoningRequest) -> RuntimeAdapter:
        """Bind provider-neutral reasoning intent without mutating a shared adapter."""

        model = self.model.model_copy(update={"reasoning_request": request})
        _ = model.reasoning_resolution
        return RuntimeAdapter(self.provider, model)

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
        provider_context = context.provider_context()
        provider_events = self.provider.stream(
            model=model,
            system_prompt=provider_context.system_prompt,
            messages=provider_context.messages,
            tools=provider_context.tools,
        )
        async for event in _interruptible(provider_events, signal=signal):
            yield event


async def _interruptible(
    events: AsyncIterator[ProviderStreamEvent],
    *,
    signal: AbortSignal | None,
    poll_interval: float = 0.05,
) -> AsyncIterator[ProviderStreamEvent]:
    """Read the provider independently from persistence and presentation.

    The reader is the sole owner of ``events`` and the consumer is the sole
    reader of the bounded FIFO. This keeps the socket moving while downstream
    awaits cooperative work, with one explicit backpressure boundary when the
    FIFO is full.
    """
    buffer: asyncio.Queue[_BufferedStreamItem] = asyncio.Queue(
        maxsize=_STREAM_BUFFER_CAPACITY
    )
    reader = asyncio.create_task(_read_provider_stream(events, buffer))
    abort_wait = (
        asyncio.create_task(_wait_for_abort(signal, poll_interval))
        if signal is not None
        else None
    )
    pending: _BufferedStreamItem | None = None

    try:
        while signal is None or not signal.is_aborted():
            item = pending
            pending = None
            if item is None:
                item = await _next_stream_item(
                    buffer,
                    abort_wait=abort_wait,
                )
                if item is None:
                    break
            if isinstance(item, _StreamEnd):
                if item.error is not None:
                    raise item.error
                break

            event, pending = _coalesce_buffered_deltas(item, buffer)
            if signal is not None and signal.is_aborted():
                break
            yield event
    finally:
        if abort_wait is not None:
            abort_wait.cancel()
            with suppress(asyncio.CancelledError):
                await abort_wait
        reader.cancel()
        with suppress(asyncio.CancelledError):
            await reader
        close = getattr(events, "aclose", None)
        if close is not None:
            with suppress(Exception):
                await close()


async def _read_provider_stream(
    events: AsyncIterator[ProviderStreamEvent],
    buffer: asyncio.Queue[_BufferedStreamItem],
) -> None:
    """Continuously pump one provider stream into its bounded FIFO."""
    try:
        async for event in events:
            await buffer.put(event)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await buffer.put(_StreamEnd(error=exc))
    else:
        await buffer.put(_StreamEnd())


async def _next_stream_item(
    buffer: asyncio.Queue[_BufferedStreamItem],
    *,
    abort_wait: asyncio.Task[None] | None,
) -> _BufferedStreamItem | None:
    try:
        return buffer.get_nowait()
    except asyncio.QueueEmpty:
        pass
    if abort_wait is None:
        return await buffer.get()

    get_item = asyncio.create_task(buffer.get())
    try:
        done, _ = await asyncio.wait(
            {get_item, abort_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if get_item in done:
            return get_item.result()
        return None
    finally:
        if not get_item.done():
            get_item.cancel()
            with suppress(asyncio.CancelledError):
                await get_item


def _coalesce_buffered_deltas(
    first: ProviderStreamEvent,
    buffer: asyncio.Queue[_BufferedStreamItem],
) -> tuple[ProviderStreamEvent, _BufferedStreamItem | None]:
    """Merge only adjacent, already-buffered plain deltas of the same kind."""
    field = _coalescible_field(first)
    if field is None:
        return first, None

    fragments = [getattr(first, field) or ""]
    length = len(fragments[0])
    pending: _BufferedStreamItem | None = None
    while length < _STREAM_COALESCE_MAX_CHARS:
        try:
            item = buffer.get_nowait()
        except asyncio.QueueEmpty:
            break
        if not isinstance(item, ProviderStreamEvent):
            pending = item
            break
        next_field = _coalescible_field(item)
        fragment = getattr(item, field) if next_field == field else None
        if fragment is None or length + len(fragment) > _STREAM_COALESCE_MAX_CHARS:
            pending = item
            break
        fragments.append(fragment)
        length += len(fragment)

    if len(fragments) == 1:
        return first, pending
    return first.model_copy(update={field: "".join(fragments)}), pending


def _coalescible_field(event: ProviderStreamEvent) -> str | None:
    if any(
        value is not None
        for value in (
            event.continuation,
            event.tool_call_delta,
            event.usage,
            event.completion,
            event.retry,
            event.error,
        )
    ):
        return None
    if event.type == "text_delta" and event.reasoning_content is None:
        return "text"
    if event.type == "reasoning_delta" and event.text is None:
        return "reasoning_content"
    return None


async def _wait_for_abort(signal: AbortSignal, poll_interval: float) -> None:
    while not signal.is_aborted():
        await asyncio.sleep(poll_interval)
