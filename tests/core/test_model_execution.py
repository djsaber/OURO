import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from ouro.core.adapter import RuntimeAdapter
from ouro.core.cancellation import AbortSignal
from ouro.core.context import ContextView
from ouro.core.messages import ProviderContinuationArtifact
from ouro.core.model_execution import ModelExecutionEvent, execute_model_request
from ouro.core.provider import (
    ModelMetadata,
    ProviderCompletion,
    ProviderStreamEvent,
    ToolCallDelta,
    Usage,
)
from ouro.core.request_context import ProviderRequestContext
from ouro.providers import FauxProvider


async def _execute(provider, *, observe=None, signal=None, **kwargs):
    async def ignore(event):
        pass

    return await execute_model_request(
        RuntimeAdapter(provider, ModelMetadata(model_id="test", provider_id="test")),
        ContextView(system_prompt="", model_messages=[], tool_definitions=[]),
        request_context=ProviderRequestContext({"request.purpose": "chat"}),
        signal=signal or AbortSignal(),
        on_event=observe or ignore,
        **kwargs,
    )


@pytest.mark.parametrize("clock_shift", [-10, 10])
@pytest.mark.parametrize("output_kind", ["text", "tool"])
@pytest.mark.asyncio
async def test_wall_clock_changes_do_not_invalidate_model_response(clock_shift, output_kind):
    started = datetime(2026, 1, 1, tzinfo=UTC)
    wall_time = started
    elapsed = 100.0

    async def observe(progress):
        nonlocal wall_time, elapsed
        wall_time = started + timedelta(
            seconds=clock_shift if progress.event.type == "reasoning_delta" else 1
        )
        elapsed += 1.25

    events = [
        ProviderStreamEvent(type="reasoning_delta", reasoning_content="plan"),
        ProviderStreamEvent(type="text_delta", text="answer")
        if output_kind == "text"
        else ProviderStreamEvent.from_tool_call(
            {"id": "call", "name": "Read", "arguments": {"path": "notes.txt"}}
        ),
        ProviderStreamEvent(type="done"),
    ]
    output = await _execute(
        FauxProvider(events),
        observe=observe,
        clock=lambda: wall_time,
        monotonic_clock=lambda: elapsed,
    )

    assert output.error is None
    assert output.reasoning == "plan"
    assert output.model_response.started_at == started
    assert output.model_response.reasoning_ended_at == started + timedelta(seconds=1.25)
    assert output.model_response.ended_at == started + timedelta(seconds=2.5)
    if output_kind == "text":
        assert output.text == "answer"
    else:
        assert output.tool_calls[0].name == "Read"


@pytest.mark.parametrize("reasoning", [False, True])
@pytest.mark.parametrize("ending", ["done", "error", "exhausted", "abort", "exception"])
@pytest.mark.asyncio
async def test_request_end_uses_elapsed_time_after_wall_clock_rollback(reasoning, ending):
    started = datetime(2026, 1, 1, tzinfo=UTC)
    wall_time = started
    elapsed = 100.0
    signal = AbortSignal()
    closed = asyncio.Event()

    class Provider(FauxProvider):
        async def stream(self, **kwargs):
            try:
                yield (
                    ProviderStreamEvent(type="reasoning_delta", reasoning_content="plan")
                    if reasoning
                    else ProviderStreamEvent(type="text_delta", text="answer")
                )
                if ending == "done":
                    yield ProviderStreamEvent(type="done")
                elif ending == "error":
                    yield ProviderStreamEvent(type="error", error="provider failed")
                elif ending == "exception":
                    raise RuntimeError("transport failed")
            finally:
                closed.set()

    async def observe(progress):
        nonlocal wall_time, elapsed
        wall_time = started - timedelta(hours=1)
        elapsed = 102.5
        if ending == "abort":
            signal.abort()

    output = await _execute(
        Provider(),
        observe=observe,
        signal=signal,
        clock=lambda: wall_time,
        monotonic_clock=lambda: elapsed,
    )

    assert closed.is_set()
    assert output.aborted == (ending == "abort")
    assert output.model_response.started_at == started
    assert output.model_response.ended_at == started + timedelta(seconds=2.5)
    assert output.model_response.reasoning_ended_at == (
        output.model_response.ended_at if reasoning else None
    )
    assert (
        output.error
        == {
            "done": "model returned reasoning without a final answer or tool call"
            if reasoning
            else None,
            "error": "provider failed",
            "exhausted": "provider stream ended without a completion event",
            "abort": None,
            "exception": "transport failed",
        }[ending]
    )


@pytest.mark.asyncio
async def test_interleaved_tool_fragments_preserve_source_order_and_unicode() -> None:
    events = [
        ProviderStreamEvent(type="reasoning_delta", reasoning_content="plan"),
        ProviderStreamEvent(type="text_delta", text="running"),
        ProviderStreamEvent(
            type="tool_call_delta",
            tool_call_delta=ToolCallDelta(index=4, id_delta="second", name_delta="Second"),
        ),
        ProviderStreamEvent(
            type="tool_call_delta",
            tool_call_delta=ToolCallDelta(
                index=0, id_delta="first", name_delta="First", arguments_delta='{"value":"'
            ),
        ),
        ProviderStreamEvent(
            type="tool_call_delta",
            tool_call_delta=ToolCallDelta(index=4, arguments_delta='{"number":2}'),
        ),
        *[
            ProviderStreamEvent(
                type="tool_call_delta", tool_call_delta=ToolCallDelta(index=0, arguments_delta=part)
            )
            for part in [*"中文🙂" * 200, '"}']
        ],
        ProviderStreamEvent(type="done", usage=Usage(input_tokens=3, output_tokens=7)),
    ]
    observed: list[ModelExecutionEvent] = []

    async def observe(progress):
        observed.append(progress)

    started = datetime(2026, 1, 1, tzinfo=UTC)
    ticks = iter([100.0, 101.0, 102.0])
    output = await _execute(
        FauxProvider(events),
        observe=observe,
        clock=lambda: started,
        monotonic_clock=lambda: next(ticks),
    )

    assert output.error is None
    assert not output.aborted
    assert output.text == "running"
    assert output.reasoning == "plan"
    assert [(c.id, c.source_index) for c in output.tool_calls] == [("first", 0), ("second", 1)]
    assert output.tool_calls[0].arguments == {"value": "中文🙂" * 200}
    assert output.tool_calls[1].arguments == {"number": 2}
    assert observed[-1].tool_call_count == 2
    assert observed[-1].has_reasoning
    assert output.usage == Usage(input_tokens=3, output_tokens=7)
    assert output.model_response.started_at == started
    assert output.model_response.reasoning_ended_at == started + timedelta(seconds=1)
    assert output.model_response.ended_at == started + timedelta(seconds=2)


@pytest.mark.parametrize("terminal", ["done", "error"])
@pytest.mark.asyncio
async def test_terminal_event_closes_stream_and_ignores_trailing_events(terminal) -> None:
    closed = asyncio.Event()

    class TrailingProvider(FauxProvider):
        async def stream(self, **kwargs) -> AsyncIterator[ProviderStreamEvent]:
            try:
                yield ProviderStreamEvent(type="text_delta", text="answer")
                yield (
                    ProviderStreamEvent(type="error", error="failed", error_code="access")
                    if terminal == "error"
                    else ProviderStreamEvent(type="done")
                )
                yield ProviderStreamEvent(type="text_delta", text="must not be consumed")
                yield ProviderStreamEvent(type="done", usage=Usage(output_tokens=999))
                await asyncio.Event().wait()
            finally:
                closed.set()

    output = await asyncio.wait_for(_execute(TrailingProvider()), timeout=1)

    assert closed.is_set()
    assert output.text == "answer"
    assert output.usage is None
    assert output.error == ("failed" if terminal == "error" else None)
    assert output.error_code == ("access" if terminal == "error" else None)


@pytest.mark.parametrize("finish_reason", ["length", "stop"])
@pytest.mark.asyncio
async def test_usage_observation_precedes_output_validation(finish_reason) -> None:
    observed = []
    continuation = ProviderContinuationArtifact(
        provider_id="test", model_id="test", protocol="test", blocks=({"opaque": True},)
    )
    events = [
        ProviderStreamEvent(type="continuation", continuation=continuation),
        ProviderStreamEvent.from_tool_call({"id": "call", "name": "Write", "arguments": {}}),
        ProviderStreamEvent(
            type="tool_call_delta", tool_call_delta=ToolCallDelta(index=0, arguments_delta="{")
        ),
        ProviderStreamEvent(
            type="done",
            completion=ProviderCompletion(finish_reason=finish_reason),
            usage=Usage(output_tokens=9),
        ),
    ]

    async def observe(progress):
        if progress.event.type == "done":
            observed.append(progress.event.usage)

    output = await _execute(FauxProvider(events), observe=observe)

    assert observed == [Usage(output_tokens=9)]
    assert output.usage == Usage(output_tokens=9)
    assert output.tool_calls == ()
    if finish_reason == "length":
        assert output.error_code == "model_output_limit"
        assert output.continuation is None
    else:
        assert "not valid JSON" in output.error


@pytest.mark.parametrize("mode", ["abort", "cancel", "observer_failure", "transport_failure"])
@pytest.mark.asyncio
async def test_interrupted_request_closes_provider_and_preserves_partial_output(mode) -> None:
    closed = asyncio.Event()
    started = asyncio.Event()
    signal = AbortSignal()

    class InterruptedProvider(FauxProvider):
        async def stream(self, **kwargs) -> AsyncIterator[ProviderStreamEvent]:
            try:
                yield ProviderStreamEvent(type="text_delta", text="partial")
                if mode == "transport_failure":
                    raise RuntimeError("transport failed")
                await asyncio.Event().wait()
            finally:
                closed.set()

    async def observe(progress):
        started.set()
        if mode == "observer_failure":
            raise RuntimeError("event persistence failed")

    task = asyncio.create_task(_execute(InterruptedProvider(), observe=observe, signal=signal))
    await asyncio.wait_for(started.wait(), timeout=1)
    if mode == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        if mode == "abort":
            signal.abort()
        output = await asyncio.wait_for(task, timeout=1)
        assert output.text == "partial"
        assert output.tool_calls == ()
        assert output.aborted == (mode == "abort")
        assert (
            output.error
            == {
                "abort": None,
                "observer_failure": "event persistence failed",
                "transport_failure": "transport failed",
            }[mode]
        )
    assert closed.is_set()


@pytest.mark.asyncio
async def test_stream_exhaustion_cannot_turn_partial_text_into_success() -> None:
    output = await _execute(FauxProvider([ProviderStreamEvent(type="text_delta", text="partial")]))
    assert output.text == "partial"
    assert output.error == "provider stream ended without a completion event"
    assert output.completion is None


@pytest.mark.asyncio
async def test_duplicate_tool_ids_reject_the_entire_batch() -> None:
    output = await _execute(
        FauxProvider(
            [
                ProviderStreamEvent.from_tool_call(
                    {"id": "duplicate", "name": name, "arguments": {}}, index=index
                )
                for index, name in enumerate(["Read", "Write"])
            ]
            + [ProviderStreamEvent(type="done")]
        )
    )
    assert output.error == "duplicate provider tool call id: duplicate"
    assert output.tool_calls == ()
