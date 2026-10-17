import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from types import SimpleNamespace

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


@pytest.fixture
def stream_clock(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    loop = SimpleNamespace(time=lambda: clock.now)
    # Only the adapter reads this clock; asyncio's scheduler and test watchdogs
    # continue to use the real event loop.
    monkeypatch.setattr(
        adapter_module,
        "asyncio",
        SimpleNamespace(**(vars(asyncio) | {"get_running_loop": lambda: loop})),
    )
    return clock


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

    async with aclosing(_interruptible(source(), signal=None)) as stream:
        first = await asyncio.wait_for(anext(stream), timeout=5)
        assert first.text == "first"
        assert not release.is_set()


@pytest.mark.asyncio
async def test_fast_subsequent_plain_deltas_share_one_fixed_flush_window(
    monkeypatch: pytest.MonkeyPatch,
    stream_clock,
) -> None:
    monkeypatch.setattr(adapter_module, "_STREAM_COALESCE_WINDOW_SECONDS", 60)
    second, third, fourth = (asyncio.Event() for _ in range(3))
    waits = []
    next_item = adapter_module._next_stream_item

    async def release_fragment(buffer, *, abort_wait, timeout=None):
        if timeout is not None:
            waits.append(timeout)
            if len(waits) == 1:
                stream_clock.now += 20
                third.set()
            elif len(waits) == 2:
                stream_clock.now += 40
                fourth.set()
            else:
                pytest.fail("new fragments extended the original flush deadline")
        return await next_item(buffer, abort_wait=abort_wait, timeout=timeout)

    monkeypatch.setattr(adapter_module, "_next_stream_item", release_fragment)

    async def source() -> AsyncIterator[ProviderStreamEvent]:
        yield ProviderStreamEvent(type="text_delta", text="first")
        await second.wait()
        yield ProviderStreamEvent(type="text_delta", text="second")
        await third.wait()
        yield ProviderStreamEvent(type="text_delta", text=" third")
        await fourth.wait()
        yield ProviderStreamEvent(type="text_delta", text=" fourth")
        await asyncio.Event().wait()

    async with aclosing(_interruptible(source(), signal=None)) as stream:
        assert (await asyncio.wait_for(anext(stream), timeout=5)).text == "first"
        assert waits == []
        second.set()
        merged = await asyncio.wait_for(anext(stream), timeout=5)
        assert merged.text == "second third fourth"
        assert waits == [60, 40]


@pytest.mark.asyncio
async def test_plain_delta_arriving_after_flush_deadline_is_not_delayed(
    monkeypatch: pytest.MonkeyPatch,
    stream_clock,
) -> None:
    monkeypatch.setattr(adapter_module, "_STREAM_COALESCE_WINDOW_SECONDS", 60)
    release = asyncio.Event()
    next_item = adapter_module._next_stream_item

    async def reject_coalescing_wait(buffer, *, abort_wait, timeout=None):
        assert timeout is None, "an expired flush deadline must not wait for more text"
        return await next_item(buffer, abort_wait=abort_wait, timeout=timeout)

    monkeypatch.setattr(adapter_module, "_next_stream_item", reject_coalescing_wait)

    async def source() -> AsyncIterator[ProviderStreamEvent]:
        yield ProviderStreamEvent(type="text_delta", text="first")
        await release.wait()
        yield ProviderStreamEvent(type="text_delta", text="second")
        await asyncio.Event().wait()

    async with aclosing(_interruptible(source(), signal=None)) as stream:
        assert (await asyncio.wait_for(anext(stream), timeout=5)).text == "first"
        stream_clock.now += 61
        release.set()
        assert (await asyncio.wait_for(anext(stream), timeout=5)).text == "second"


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
