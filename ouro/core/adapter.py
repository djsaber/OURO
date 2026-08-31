from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass

from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextView
from ouro.core.provider import ModelMetadata, Provider, ProviderStreamEvent
from ouro.core.reasoning import ReasoningRequest

_STREAM_BUFFER_CAPACITY = 256
_STREAM_COALESCE_MAX_CHARS = 16 * 1024
_STREAM_COALESCE_WINDOW_SECONDS = 0.008


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
        on_stream_start: Callable[[], None] | None = None,
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
        async for event in _interruptible(
            provider_events,
            signal=signal,
            on_stream_start=on_stream_start,
        ):
            yield event


async def _interruptible(
    events: AsyncIterator[ProviderStreamEvent],
    *,
    signal: AbortSignal | None,
    on_stream_start: Callable[[], None] | None = None,
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
    reader = asyncio.create_task(
        _read_provider_stream(events, buffer, on_stream_start=on_stream_start)
    )
    abort_wait = (
        asyncio.create_task(signal.wait(poll_interval=poll_interval))
        if signal is not None
        else None
    )
    pending: _BufferedStreamItem | None = None
    next_plain_flush_at: dict[str, float] = {}
    last_plain_field: str | None = None

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

            field = _coalescible_field(item)
            if field is None:
                next_plain_flush_at.clear()
                last_plain_field = None
            elif field != last_plain_field:
                next_plain_flush_at.clear()
                last_plain_field = field
            event, pending = await _coalesce_plain_deltas(
                item,
                buffer,
                abort_wait=abort_wait,
                flush_at=(next_plain_flush_at.get(field) if field is not None else None),
            )
            if signal is not None and signal.is_aborted():
                break
            if field is not None:
                next_plain_flush_at[field] = (
                    asyncio.get_running_loop().time() + _STREAM_COALESCE_WINDOW_SECONDS
                )
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
    *,
    on_stream_start: Callable[[], None] | None = None,
) -> None:
    """Continuously pump one provider stream into its bounded FIFO."""
    try:
        if on_stream_start is not None:
            on_stream_start()
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
    timeout: float | None = None,
) -> _BufferedStreamItem | None:
    try:
        return buffer.get_nowait()
    except asyncio.QueueEmpty:
        pass
    get_item = asyncio.create_task(buffer.get())
    try:
        waiters: set[asyncio.Task[object]] = {get_item}
        if abort_wait is not None:
            waiters.add(abort_wait)
        done, _ = await asyncio.wait(
            waiters,
            timeout=timeout,
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


async def _coalesce_plain_deltas(
    first: ProviderStreamEvent,
    buffer: asyncio.Queue[_BufferedStreamItem],
    *,
    abort_wait: asyncio.Task[None] | None,
    flush_at: float | None,
) -> tuple[ProviderStreamEvent, _BufferedStreamItem | None]:
    """Merge adjacent plain deltas up to one fixed, non-extending deadline."""
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
            if flush_at is None:
                break
            remaining = flush_at - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            next_item = await _next_stream_item(
                buffer,
                abort_wait=abort_wait,
                timeout=remaining,
            )
            if next_item is None:
                break
            item = next_item
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
