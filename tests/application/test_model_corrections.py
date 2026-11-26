import asyncio
import json

import httpx
import pytest

from ouro.application.runtime.service import RuntimeService
from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import (
    ModelMetadata,
    ProviderCompletion,
    ProviderStreamEvent,
    ToolCallDelta,
    Usage,
)
from ouro.core.run_budget import RunBudgetPolicy
from ouro.providers import AnthropicMessagesProvider, FauxProvider, OpenAIChatCompletionsProvider
from ouro.providers.http_transport import HttpxProviderHTTPTransport
from ouro.sessions.backends.files.store import LocalSessionStore
from ouro.sessions.backends.sqlite.store import SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult
from tests.event_capture import recorded_events


class CorrectingProvider(FauxProvider):
    def __init__(self, failures):
        super().__init__()
        self.failures = failures
        self.requests = []

    async def stream(self, *, messages, **kwargs):
        self.requests.append([message.model_dump(mode="json") for message in messages])
        round_number = len(self.requests)
        if round_number <= self.failures:
            yield ProviderStreamEvent(type="text_delta", text="rejected preamble")
            yield ProviderStreamEvent(type="reasoning_delta", reasoning_content="private thought")
            yield ProviderStreamEvent(
                type="tool_call_delta",
                tool_call_delta=ToolCallDelta(
                    index=0,
                    id_delta=f"bad-{round_number}",
                    name_delta="Act",
                    arguments_delta='{"value":"line\nbreak"}',
                ),
            )
            yield ProviderStreamEvent(
                type="tool_call_delta",
                tool_call_delta=ToolCallDelta(
                    index=1,
                    id_delta="sibling",
                    name_delta="Act",
                    arguments_delta="{}",
                ),
            )
        elif not any(message.tool_result for message in messages):
            yield ProviderStreamEvent.from_tool_call(
                {"id": "valid", "name": "Act", "arguments": {}}
            )
        else:
            yield ProviderStreamEvent(type="text_delta", text="done")
        yield ProviderStreamEvent(type="done", usage=Usage(input_tokens=10, output_tokens=2))


@pytest.fixture(params=[LocalSessionStore, SQLiteSessionStore])
def store(tmp_path, request):
    value = request.param(
        tmp_path / "session.db" if request.param is SQLiteSessionStore else tmp_path / "sessions"
    )
    value.initialize()
    return value


def runtime_for(store, provider, calls, tmp_path, *, streaming=True, **kwargs):
    tools = ToolRegistry()

    def act(invocation, context):
        calls.append(invocation.tool_call_id)
        return ToolResult(tool_call_id=invocation.tool_call_id, tool_name="Act", content="acted")

    tools.register(Tool(definition=ToolDefinition(name="Act", description="Act"), handler=act))
    return RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(
                model_id="test",
                provider_id="test",
                supports_streaming=streaming,
                max_output_tokens=1024,
            ),
        ),
        tools=tools,
        usage_service=UsageService(LocalUsageLedger(tmp_path / "billing")),
        **kwargs,
    )


@pytest.mark.parametrize("failures", [1, 5])
async def test_agent_can_correct_its_call_after_repeated_rejections(store, tmp_path, failures):
    provider = CorrectingProvider(failures)
    calls = []
    async with runtime_for(store, provider, calls, tmp_path) as runtime:
        result = await runtime.run("act")
        assert result.run.status == "completed"
        assert calls == ["valid"]
        assert len(provider.requests) == failures + 2
        assert result.usage["output_tokens"] == 2 * len(provider.requests)
        assert result.run.model_round_count == len(provider.requests)
        for before, after in zip(provider.requests, provider.requests[1:], strict=False):
            assert after[: len(before)] == before
        snapshot = store.load_session(result.session.session_id)
        corrections = [m for m in snapshot.messages if m.semantic_tag == "model-output-correction"]
        assert len(corrections) == failures
        assert all(m.role == "harness" for m in corrections)
        assert (
            corrections[0].metadata["rejected_response"]["tool_call_failure"]["arguments"]
            == '{"value":"line\nbreak"}'
        )
        assert all(m.text_content != "rejected preamble" for m in snapshot.messages)
        assert all(h.response_closed_at for h in snapshot.request_headers)
        assert (
            store.load_response_draft(
                result.session.session_id, snapshot.request_headers[-1].context_id
            )
            is None
        )
        for request in provider.requests:
            assert all(not m.get("provider_continuation") for m in request)
            assert all(c["id"] == "valid" for m in request for c in m["tool_calls"])


async def test_unaddressable_tool_frames_return_facts_and_the_agent_can_finish(store, tmp_path):
    def proposal(call_id, name, index=0):
        return ProviderStreamEvent(
            type="tool_call_delta",
            tool_call_delta=ToolCallDelta(index=index, id_delta=call_id, name_delta=name),
        )

    done = ProviderStreamEvent(type="done")
    provider = FauxProvider(
        event_batches=[
            [proposal("", "Act"), done],
            [proposal("", ""), proposal("sibling", "Act", index=1), done],
            [proposal("a", ""), done],
            [proposal("a", "Act"), proposal("a", "Act", index=1), done],
            [
                ProviderStreamEvent(
                    type="done", completion=ProviderCompletion(finish_reason="tool_call")
                )
            ],
            [ProviderStreamEvent(type="text_delta", text="I can answer directly."), done],
        ]
    )
    calls = []
    async with runtime_for(store, provider, calls, tmp_path) as runtime:
        result = await runtime.run("act")
        assert result.run.status == "completed"
        assert result.final_text == "I can answer directly."
        assert result.run.model_round_count == 6
        assert calls == []
        corrections = [
            m
            for m in store.load_session(result.session.session_id).messages
            if m.semantic_tag == "model-output-correction"
        ]
        assert len(corrections) == 5
        for correction, reason in zip(
            corrections,
            [
                "id must not be empty",
                "id must not be empty",
                "name must not be empty",
                "duplicate",
                "returned no tool call",
            ],
            strict=True,
        ):
            assert reason in correction.text_content
            assert "No tool calls from that response were executed." in correction.text_content


async def test_correction_survives_budget_pause_and_continue(store, tmp_path):
    provider = CorrectingProvider(2)
    calls = []
    async with runtime_for(
        store, provider, calls, tmp_path, run_budget=RunBudgetPolicy(max_model_rounds=1)
    ) as runtime:
        result = await runtime.run("act")
        assert result.run.status == "paused"
        assert len(provider.requests) == 1
        assert calls == []
    store.save_run(
        result.run.model_copy(
            update={
                "metadata": {
                    **result.run.metadata,
                    "model_correction_attempt": 99,
                    "model_correction_round": result.run.model_round_count,
                    "run_budget": RunBudgetPolicy(max_model_rounds=10).model_dump(mode="json"),
                }
            }
        )
    )
    async with runtime_for(store, provider, calls, tmp_path) as runtime:
        result = await runtime.continue_session(session_id=result.session.session_id)
        assert result.run.status == "completed"
        assert calls == ["valid"]
        assert provider.requests[1][: len(provider.requests[0])] == provider.requests[0]
        corrections = [
            m
            for m in store.load_session(result.session.session_id).messages
            if m.semantic_tag == "model-output-correction"
        ]
        assert len(corrections) == 2


async def test_cancel_after_correction_does_not_dispatch_retry(store, tmp_path, monkeypatch):
    provider = CorrectingProvider(1)
    calls = []
    committed = asyncio.Event()
    release = asyncio.Event()
    async with runtime_for(store, provider, calls, tmp_path) as runtime:
        original = runtime.run_lifecycle.record_unexecuted_model_tools

        async def correct(*args):
            session, run = await original(*args)
            events = recorded_events(session.session_id)
            repeated = await original(*args)
            assert repeated == (session, run)
            assert recorded_events(session.session_id) == events
            committed.set()
            await release.wait()
            return session, run

        monkeypatch.setattr(runtime.run_lifecycle, "record_unexecuted_model_tools", correct)
        task = asyncio.create_task(runtime.run("act"))
        await asyncio.wait_for(committed.wait(), timeout=10)
        session = store.list_sessions()[0]
        signal = runtime.run_ownership.signal_for(session.active_run_id)
        abort = asyncio.create_task(runtime.abort_run(session_id=session.session_id))
        await asyncio.wait_for(signal.wait(), timeout=10)
        release.set()
        result = await asyncio.wait_for(task, timeout=10)
        await abort
        assert result.run.status == "aborted"
        assert len(provider.requests) == 1
        assert calls == []


@pytest.mark.parametrize("streaming", [True, False])
async def test_wire_retry_sends_invalid_arguments_only_as_user_text(tmp_path, streaming):
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        message = (
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "bad",
                        "type": "function",
                        "function": {
                            "name": "Act",
                            "arguments": '{"value":"line\nbreak"}',
                        },
                    }
                ]
            }
            if len(requests) == 1
            else {"content": "corrected"}
        )
        body = {
            "choices": [
                {
                    "delta" if streaming else "message": message,
                    "finish_reason": "tool_calls" if len(requests) == 1 else "stop",
                }
            ]
        }
        return (
            httpx.Response(
                200,
                text="data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
            if streaming
            else httpx.Response(200, json=body)
        )

    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIChatCompletionsProvider(
            base_url="https://example.test", transport=HttpxProviderHTTPTransport(client=client)
        )
        async with runtime_for(store, provider, [], tmp_path, streaming=streaming) as runtime:
            result = await runtime.run("act")
            assert result.run.status == "completed"
    assert len(requests) == 2
    before, after = requests[0]["messages"], requests[1]["messages"]
    assert after[: len(before)] == before
    assert after[-1]["role"] == "user"
    assert "line\nbreak" in str(after[-1]["content"])
    assert not any(message.get("tool_calls") for message in after)


@pytest.mark.parametrize(
    "provider_type", [OpenAIChatCompletionsProvider, AnthropicMessagesProvider]
)
@pytest.mark.parametrize("streaming", [True, False])
async def test_wire_adapters_preserve_malformed_calls_alongside_valid_siblings(
    tmp_path, provider_type, streaming
):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content)["messages"])
        round_number = len(requests)
        bad_arguments = round_number == 1
        has_tools = round_number <= 2
        if provider_type is AnthropicMessagesProvider:
            blocks = (
                [
                    {"type": "tool_use", "name": "Act", "id": "bad", "input": []}
                    if bad_arguments
                    else {"type": "tool_use"},
                    {"type": "tool_use", "name": "Act", "id": "sibling", "input": {}},
                ]
                if has_tools
                else [{"type": "text", "text": "done"}]
            )
            reason = "tool_use" if has_tools else "end_turn"
            body = {"content": blocks, "stop_reason": reason}
            events = []
            for index, block in enumerate(blocks):
                start = {**block, "text": ""} if block["type"] == "text" else block
                events.append(
                    {"type": "content_block_start", "index": index, "content_block": start}
                )
                if block["type"] == "text":
                    events.append(
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "text_delta", "text": "done"},
                        }
                    )
            events.extend(
                [
                    {"type": "message_delta", "delta": {"stop_reason": reason}},
                    {"type": "message_stop"},
                ]
            )
        else:
            calls = [
                {"index": 0, "id": "bad", "function": {"name": "Act", "arguments": []}}
                if bad_arguments
                else {"index": 0},
                {"index": 1, "id": "sibling", "function": {"name": "Act", "arguments": "{}"}},
            ]
            msg = {"tool_calls": calls} if has_tools else {"content": "done"}
            body = {
                "choices": [
                    {
                        "delta" if streaming else "message": msg,
                        "finish_reason": "tool_calls" if has_tools else "stop",
                    }
                ]
            }
            events = [body]
        if streaming:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="".join(f"data: {json.dumps(e)}\n\n" for e in events),
            )
        return httpx.Response(200, json=body)

    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = provider_type(
            base_url="https://example.test", transport=HttpxProviderHTTPTransport(client=client)
        )
        async with runtime_for(store, provider, calls, tmp_path, streaming=streaming) as runtime:
            result = await runtime.run("act")
    assert result.run.status == "completed", result.run.error
    assert calls == []
    assert result.run.model_round_count == len(requests) == 3
    for before, after in zip(requests, requests[1:], strict=False):
        assert after[: len(before)] == before
    feedback = [
        m
        for m in store.load_session(result.session.session_id).messages
        if m.semantic_tag == "model-output-correction"
    ]
    assert [m.metadata["rejected_response"]["error_code"] for m in feedback] == [
        "model_tool_arguments_invalid",
        "model_tool_identity_invalid",
    ]
