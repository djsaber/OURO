from __future__ import annotations

import asyncio
import json
from contextlib import aclosing

import httpx
import pytest

from ouro.core.provider import ModelMetadata, ProviderRetryPolicy
from ouro.core.reasoning import ReasoningCapability
from ouro.providers.http_transport import HttpxProviderHTTPTransport, ProviderResponseTimeout
from ouro.providers.openai_chat_completions import OpenAIChatCompletionsProvider
from ouro.providers.openai_responses import OpenAIResponsesProvider
from tests.providers.responses_fixtures import call, delta, item_event, message, reasoning, terminal
from tests.providers.test_request_authentication import PROVIDERS

MODEL = ModelMetadata(max_output_tokens=8192, model_id="model", provider_id="fixture")
TIMEOUT = 0.2
INTERVAL = 0.04


class ResponseBody(httpx.AsyncByteStream):
    def __init__(self, chunks, *, interval=INTERVAL, stall=False):
        self.chunks = chunks
        self.interval = interval
        self.stall = stall
        self.closed = False
        self.sent = 0
        self.stalled = asyncio.Event()

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.interval)
            self.sent += 1
            yield f"data: {json.dumps(chunk)}\n\n".encode()
        if self.stall:
            self.stalled.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


def response_chunks(provider, lane, text="abcdefgh"):
    if provider is OpenAIChatCompletionsProvider:
        field = "reasoning_content" if "reasoning" in lane else "content"
        if lane == "tool":
            arguments = json.dumps({"content": text})
            chunks = [
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {"name": "echo", "arguments": ""},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
            chunks += [
                {
                    "choices": [
                        {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": part}}]}}
                    ]
                }
                for part in arguments
            ]
        else:
            chunks = [
                {"choices": []} if lane == "lifecycle" else {"choices": [{"delta": {field: part}}]}
                for part in text
            ]
        chunks.append(
            {
                "choices": [
                    {
                        "delta": {"content": text} if lane == "lifecycle" else {},
                        "finish_reason": "tool_calls" if lane == "tool" else "stop",
                    }
                ]
            }
        )
        return chunks
    if provider is not OpenAIResponsesProvider:
        if lane == "tool":
            chunks = [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "call-1", "name": "echo"},
                }
            ]
            chunks += [
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": part},
                }
                for part in json.dumps({"content": text})
            ]
        else:
            field = "thinking" if "reasoning" in lane else "text"
            chunks = [
                {"type": "ping"}
                if lane == "lifecycle"
                else {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": field + "_delta", field: part},
                }
                for part in text
            ]
            if lane == "lifecycle":
                chunks.append(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    }
                )
        chunks += [
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use" if lane == "tool" else "end_turn"},
            },
            {"type": "message_stop"},
        ]
        return chunks
    if lane == "tool":
        output = call(arguments=json.dumps({"content": text}))
        chunks = [item_event(0, {**output, "arguments": ""}, complete=False)]
        chunks += [
            delta("response.function_call_arguments.delta", 0, output["id"], part)
            for part in output["arguments"]
        ]
    elif lane in {"reasoning", "hidden_reasoning"}:
        output = reasoning(text)
        chunks = [
            delta("response.reasoning_summary_text.delta", 0, "rs", part, summary_index=0)
            for part in text
        ]
    else:
        output = message(text)
        chunks = [
            {"type": "response.in_progress"}
            if lane == "lifecycle"
            else delta("response.output_text.delta", 0, "msg", part, content_index=0)
            for part in text
        ]
    chunks.append(terminal([output]))
    return chunks


@pytest.mark.parametrize("provider_class", PROVIDERS)
@pytest.mark.parametrize("lane", ["text", "tool", "reasoning", "hidden_reasoning", "lifecycle"])
async def test_active_stream_can_exceed_timeout_regardless_of_display_lane(provider_class, lane):
    text = "abcdefgh"
    model = MODEL.model_copy(
        update={
            "reasoning": ReasoningCapability(output="summary" if lane == "reasoning" else "none")
        }
    )
    body = ResponseBody(response_chunks(provider_class, lane, text))
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = provider_class(
            base_url="https://fixture.invalid",
            transport=HttpxProviderHTTPTransport(client),
            timeout=TIMEOUT,
            retry_policy=ProviderRetryPolicy(max_attempts=1),
        )
        events = [event async for event in provider.stream(model=model, messages=[])]
        assert events[-1].type == "done", events[-1]
        assert not client.is_closed
    assert len(requests) == 1 and body.closed
    assert body.sent * INTERVAL > TIMEOUT
    if lane == "tool":
        assert "".join(
            event.tool_call_delta.arguments_delta for event in events if event.tool_call_delta
        ) == json.dumps({"content": text})
    elif lane in {"reasoning", "hidden_reasoning"}:
        assert "".join(event.reasoning_content or "" for event in events) == (
            text if lane == "reasoning" else ""
        )
    else:
        assert "".join(event.text or "" for event in events) == text


@pytest.mark.parametrize("provider_class", PROVIDERS)
async def test_consumer_pause_does_not_spend_next_event_wait_budget(provider_class):
    body = ResponseBody(response_chunks(provider_class, "text", "hi"))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body))
    ) as client:
        provider = provider_class(
            base_url="https://fixture.invalid",
            transport=HttpxProviderHTTPTransport(client),
            timeout=TIMEOUT,
        )
        async with aclosing(provider.stream(model=MODEL, messages=[])) as stream:
            assert (await anext(stream)).text == "h"
            await asyncio.sleep(TIMEOUT * 2)
            events = [event async for event in stream]
        assert events[-1].type == "done", events[-1]
    assert body.closed


@pytest.mark.parametrize("after_terminal", [False, True])
async def test_stall_after_long_tool_stream_closes_without_execution_boundary_or_replay(
    after_terminal,
):
    output = call()
    chunks = [item_event(0, {**output, "arguments": ""}, complete=False)]
    chunks += [
        delta("response.function_call_arguments.delta", 0, output["id"], part)
        for part in output["arguments"]
    ]
    if after_terminal:
        chunks.append(terminal([output]))
    body = ResponseBody(chunks, stall=True)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIResponsesProvider(
            base_url="https://fixture.invalid",
            transport=HttpxProviderHTTPTransport(client),
            timeout=TIMEOUT,
            retry_policy=ProviderRetryPolicy(max_attempts=2, initial_backoff_seconds=0),
        )
        events = await asyncio.wait_for(_collect(provider), 3)
    assert body.sent == len(chunks)
    assert body.closed and len(requests) == 1
    assert events[-1].error_code == "provider_response_timeout"
    assert not any(event.type in {"done", "continuation", "retry_scheduled"} for event in events)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("provider_class", PROVIDERS)
async def test_no_response_times_out_and_retries_with_a_fresh_budget(streaming, provider_class):
    bodies = []

    def respond(request):
        body = ResponseBody([], stall=True)
        bodies.append(body)
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = provider_class(
            base_url="https://fixture.invalid",
            transport=HttpxProviderHTTPTransport(client),
            timeout=0.03,
            retry_policy=ProviderRetryPolicy(max_attempts=2, initial_backoff_seconds=0),
        )
        events = await asyncio.wait_for(
            _collect(provider, MODEL.model_copy(update={"supports_streaming": streaming})), 2
        )
    assert [event.type for event in events] == ["retry_scheduled", "error"]
    assert events[-1].error_code == "provider_response_timeout"
    assert len(bodies) == 2 and all(body.closed for body in bodies)


@pytest.mark.parametrize("provider_class", PROVIDERS)
@pytest.mark.parametrize("lane", ["text", "tool", "hidden_reasoning"])
@pytest.mark.parametrize("private_headers", [False, True])
async def test_stalled_output_has_common_safe_error_without_replay(
    provider_class, lane, private_headers
):
    bodies = []

    def respond(request):
        body = ResponseBody(response_chunks(provider_class, lane)[:2], interval=0, stall=True)
        bodies.append(body)
        return httpx.Response(200, stream=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = provider_class(
            base_url="https://fixture.invalid",
            transport=HttpxProviderHTTPTransport(client),
            timeout=0.03,
            headers={"x-private": {"source": "literal", "value": "secret"}}
            if private_headers
            else None,
            retry_policy=ProviderRetryPolicy(max_attempts=2, initial_backoff_seconds=0),
        )
        events = await asyncio.wait_for(_collect(provider), 2)
    assert events[-1].error == "Model response timed out after 0.03 seconds of inactivity."
    assert events[-1].error_code == "provider_response_timeout"
    assert not any(event.type in {"done", "retry_scheduled", "continuation"} for event in events)
    assert len(bodies) == 1 and bodies[0].closed
    assert "secret" not in repr(events)


@pytest.mark.parametrize("provider_class", PROVIDERS)
@pytest.mark.parametrize("cancel", [False, True])
async def test_cancelling_or_closing_stream_releases_response_immediately(provider_class, cancel):
    body = ResponseBody(response_chunks(provider_class, "text")[:1], interval=0, stall=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body))
    ) as client:
        provider = provider_class(
            base_url="https://fixture.invalid",
            transport=HttpxProviderHTTPTransport(client),
            timeout=10,
        )
        if cancel:
            task = asyncio.create_task(_collect(provider))
            await asyncio.wait_for(body.stalled.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            async with aclosing(provider.stream(model=MODEL, messages=[])) as stream:
                assert (await anext(stream)).text == "a"
        assert body.closed
        assert not client.is_closed


@pytest.mark.parametrize("provider_class", PROVIDERS)
@pytest.mark.parametrize("streaming", [False, True])
async def test_wait_for_response_headers_uses_common_timeout(provider_class, streaming):
    cancelled = asyncio.Event()

    async def respond(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = provider_class(
            base_url="https://fixture.invalid",
            transport=HttpxProviderHTTPTransport(client),
            timeout=0.03,
            retry_policy=ProviderRetryPolicy(max_attempts=1),
        )
        events = await asyncio.wait_for(
            _collect(provider, MODEL.model_copy(update={"supports_streaming": streaming})), 2
        )
    assert [event.type for event in events] == ["error"]
    assert events[-1].error_code == "provider_response_timeout"
    assert cancelled.is_set()


@pytest.mark.parametrize("fragment", [b": heartbeat\n\n", b'data: {"partial": '])
async def test_sse_comments_and_incomplete_events_do_not_extend_wait(fragment):
    class IncompleteBody(ResponseBody):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.005)
                yield fragment

    body = IncompleteBody([])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body))
    ) as client:
        transport = HttpxProviderHTTPTransport(client)
        async with aclosing(
            transport.stream_json(
                url="https://fixture.invalid", headers={}, payload={}, timeout=0.03
            )
        ) as stream:
            with pytest.raises(ProviderResponseTimeout):
                await asyncio.wait_for(anext(stream), 1)
    assert body.closed


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
@pytest.mark.parametrize("streaming", [False, True])
async def test_transport_rejects_invalid_wait_budget_before_network(timeout, streaming):
    def respond(request):
        pytest.fail("invalid timeout must not send a request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = HttpxProviderHTTPTransport(client)
        options = dict(url="https://fixture.invalid", headers={}, payload={}, timeout=timeout)
        with pytest.raises(ValueError, match="positive and finite"):
            if streaming:
                await anext(transport.stream_json(**options))
            else:
                await transport.post_json(**options)


async def _collect(provider, model=MODEL):
    return [event async for event in provider.stream(model=model, messages=[])]
