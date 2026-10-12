import asyncio
from collections.abc import AsyncIterator

import pytest

import ouro.core.adapter as adapter_module
from ouro.core.adapter import (
    _STREAM_BUFFER_CAPACITY,
    _STREAM_COALESCE_MAX_CHARS,
    RuntimeAdapter,
    _interruptible,
)
from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextView
from ouro.core.provider import ModelMetadata, ProviderStreamEvent, ToolCallDelta
from ouro.providers import FauxProvider


def _tool_delta(index: int) -> ProviderStreamEvent:
    return ProviderStreamEvent(
        type="tool_call_delta",
        tool_call_delta=ToolCallDelta(index=0, arguments_delta=str(index)),
    )


@pytest.mark.asyncio
async def test_adapter_close_waits_for_provider_resource_release() -> None:
    closed = asyncio.Event()

    class OpenProvider(FauxProvider):
        async def stream(self, **kwargs) -> AsyncIterator[ProviderStreamEvent]:
            try:
                yield _tool_delta(0)
                await asyncio.Event().wait()
            finally:
                closed.set()

    adapter = RuntimeAdapter(OpenProvider(), ModelMetadata(model_id="test", provider_id="test"))
    stream = adapter.stream(ContextView(system_prompt="", model_messages=[], tool_definitions=[]))
    assert (await anext(stream)).type == "tool_call_delta"

    await stream.aclose()

    assert closed.is_set()


@pytest.mark.asyncio
async def test_stream_start_notifier_precedes_provider_iteration() -> None:
    order: list[str] = []

    async def source() -> AsyncIterator[ProviderStreamEvent]:
        order.append("provider")
        yield ProviderStreamEvent(type="done")

    received = [
        event
        async for event in _interruptible(
            source(),
            signal=None,
            on_stream_start=lambda: order.append("notifier"),
        )
    ]

    assert order == ["notifier", "provider"]
    assert [event.type for event in received] == ["done"]


@pytest.mark.asyncio
async def test_stream_reader_runs_ahead_but_remains_bounded() -> None:
    produced = 0
    closed = asyncio.Event()

    async def source() -> AsyncIterator[ProviderStreamEvent]:
        nonlocal produced
        try:
            for index in range(_STREAM_BUFFER_CAPACITY * 4):
                produced += 1
                yield _tool_delta(index)
            yield ProviderStreamEvent(type="done")
        finally:
            closed.set()

    stream = _interruptible(source(), signal=None)
    first = await anext(stream)
    await asyncio.sleep(0)

    assert first.tool_call_delta is not None
    assert first.tool_call_delta.arguments_delta == "0"
    assert produced > 1
    # One event is in the consumer and one may already have been obtained from
    # the source while its queue put waits; the FIFO itself never exceeds the
    # configured capacity.
    assert produced <= _STREAM_BUFFER_CAPACITY + 2

    await stream.aclose()
    await asyncio.wait_for(closed.wait(), timeout=1)


@pytest.mark.asyncio
async def test_full_stream_buffer_preserves_every_structural_event_in_order() -> None:
    event_count = _STREAM_BUFFER_CAPACITY * 2 + 17

    async def source() -> AsyncIterator[ProviderStreamEvent]:
        for index in range(event_count):
            yield _tool_delta(index)
        yield ProviderStreamEvent(type="done")

    received = [event async for event in _interruptible(source(), signal=None)]

    assert [event.type for event in received[:-1]] == ["tool_call_delta"] * event_count
    assert received[-1].type == "done"
    assert [
        event.tool_call_delta.arguments_delta
        for event in received[:-1]
        if event.tool_call_delta is not None
    ] == [str(index) for index in range(event_count)]


@pytest.mark.asyncio
async def test_backlogged_plain_deltas_coalesce_without_crossing_boundaries() -> None:
    async def source() -> AsyncIterator[ProviderStreamEvent]:
        yield ProviderStreamEvent(type="text_delta", text="hel")
        yield ProviderStreamEvent(type="text_delta", text="lo")
        yield ProviderStreamEvent(type="reasoning_delta", reasoning_content="think")
        yield ProviderStreamEvent(type="reasoning_delta", reasoning_content="ing")
        yield _tool_delta(0)
        yield ProviderStreamEvent(type="text_delta", text="done")
        yield ProviderStreamEvent(type="done")

    received = [event async for event in _interruptible(source(), signal=None)]

    assert [event.type for event in received] == [
        "text_delta",
        "reasoning_delta",
        "tool_call_delta",
        "text_delta",
        "done",
    ]
    assert received[0].text == "hello"
    assert received[1].reasoning_content == "thinking"
    assert received[2].tool_call_delta is not None
    assert received[3].text == "done"


@pytest.mark.asyncio
async def test_first_plain_delta_never_waits_for_a_future_delta() -> None:
    release = asyncio.Event()

    async def source() -> AsyncIterator[ProviderStreamEvent]:
        yield ProviderStreamEvent(type="text_delta", text="first")
        await release.wait()
        yield ProviderStreamEvent(type="text_delta", text="second")
        yield ProviderStreamEvent(type="done")

    stream = _interruptible(source(), signal=None)
    first = await asyncio.wait_for(anext(stream), timeout=0.1)

    assert first.text == "first"
    await stream.aclose()


@pytest.mark.asyncio
async def test_fast_subsequent_plain_deltas_share_one_fixed_flush_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter_module, "_STREAM_COALESCE_WINDOW_SECONDS", 0.05)
    release = asyncio.Event()

    async def source() -> AsyncIterator[ProviderStreamEvent]:
        yield ProviderStreamEvent(type="text_delta", text="first")
        await release.wait()
        yield ProviderStreamEvent(type="text_delta", text="second")
        await asyncio.sleep(0.01)
        yield ProviderStreamEvent(type="text_delta", text=" third")
        yield ProviderStreamEvent(type="done")

    stream = _interruptible(source(), signal=None)
    assert (await asyncio.wait_for(anext(stream), timeout=0.1)).text == "first"

    release.set()
    merged = await asyncio.wait_for(anext(stream), timeout=0.1)

    assert merged.text == "second third"
    assert (await anext(stream)).type == "done"


@pytest.mark.asyncio
async def test_plain_delta_arriving_after_flush_deadline_is_not_delayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = 0.05
    monkeypatch.setattr(adapter_module, "_STREAM_COALESCE_WINDOW_SECONDS", window)
    second_ready = asyncio.Event()

    async def source() -> AsyncIterator[ProviderStreamEvent]:
        yield ProviderStreamEvent(type="text_delta", text="first")
        await asyncio.sleep(window * 1.5)
        second_ready.set()
        yield ProviderStreamEvent(type="text_delta", text="second")
        await asyncio.Event().wait()

    stream = _interruptible(source(), signal=None)
    assert (await anext(stream)).text == "first"
    await asyncio.wait_for(second_ready.wait(), timeout=0.2)

    started = asyncio.get_running_loop().time()
    assert (await asyncio.wait_for(anext(stream), timeout=window / 2)).text == "second"
    assert asyncio.get_running_loop().time() - started < window / 2
    await stream.aclose()


@pytest.mark.asyncio
async def test_delta_coalescing_respects_the_character_bound() -> None:
    async def source() -> AsyncIterator[ProviderStreamEvent]:
        yield ProviderStreamEvent(
            type="text_delta",
            text="a" * _STREAM_COALESCE_MAX_CHARS,
        )
        yield ProviderStreamEvent(type="text_delta", text="b")
        yield ProviderStreamEvent(type="done")

    received = [event async for event in _interruptible(source(), signal=None)]

    assert [event.type for event in received] == [
        "text_delta",
        "text_delta",
        "done",
    ]
    assert [event.text for event in received[:-1]] == [
        "a" * _STREAM_COALESCE_MAX_CHARS,
        "b",
    ]


@pytest.mark.asyncio
async def test_reader_exception_crosses_buffer_after_preceding_events() -> None:
    failure = RuntimeError("transport failed")

    async def source() -> AsyncIterator[ProviderStreamEvent]:
        yield _tool_delta(0)
        raise failure

    stream = _interruptible(source(), signal=None)
    assert (await anext(stream)).type == "tool_call_delta"
    with pytest.raises(RuntimeError) as raised:
        await anext(stream)
    assert raised.value is failure


@pytest.mark.asyncio
async def test_abort_closes_reader_and_provider_iterator() -> None:
    closed = asyncio.Event()
    signal = AbortSignal()

    async def source() -> AsyncIterator[ProviderStreamEvent]:
        try:
            yield _tool_delta(0)
            await asyncio.Event().wait()
        finally:
            closed.set()

    stream = _interruptible(source(), signal=signal, poll_interval=0.001)
    assert (await anext(stream)).type == "tool_call_delta"
    signal.abort()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    await asyncio.wait_for(closed.wait(), timeout=1)


@pytest.mark.asyncio
async def test_consumer_task_cancellation_closes_provider_iterator() -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    async def source() -> AsyncIterator[ProviderStreamEvent]:
        try:
            started.set()
            await asyncio.Event().wait()
            yield ProviderStreamEvent(type="done")
        finally:
            closed.set()

    stream = _interruptible(source(), signal=None)
    next_event = asyncio.create_task(anext(stream))
    await asyncio.wait_for(started.wait(), timeout=1)
    next_event.cancel()
    with pytest.raises(asyncio.CancelledError):
        await next_event
    await asyncio.wait_for(closed.wait(), timeout=1)
