import json
from pathlib import Path

import httpx
import pytest

from ouro.application.runtime import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.messages import ProviderContinuationArtifact
from ouro.core.provider import (
    ModelMetadata,
    ProviderCompletion,
    ProviderFinishReason,
    ProviderStreamEvent,
    ToolCallDelta,
    Usage,
)
from ouro.providers import FauxProvider, OpenAIChatCompletionsProvider
from ouro.providers.http_transport import HttpxProviderHTTPTransport
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry, builtin_tools


@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
@pytest.mark.parametrize(
    ("reason", "text", "reasoning", "code"),
    [
        ("length", "partial answer", "thinking", "model_output_limit"),
        ("length", "", "thinking", "model_output_limit"),
        ("length", "", "", "model_output_limit"),
        ("content_filter", "partial answer", "", "model_content_filtered"),
        ("content_filter", "", "", "model_content_filtered"),
        ("other", "partial answer", "", "model_incomplete"),
        ("tool_call", "preamble", "", "model_missing_tool_call"),
        ("stop", "", "thinking", "model_reasoning_only"),
        ("stop", "", "", "model_empty_response"),
        ("stop", " \n\t", "", "model_empty_response"),
    ],
)
async def test_unsuccessful_completion_preserves_output_and_usage(
    tmp_path, store_type, reason: ProviderFinishReason, text, reasoning, code
) -> None:
    store = store_type(
        tmp_path / "sessions.sqlite3" if store_type is SQLiteSessionStore else tmp_path
    )
    store.initialize()
    events = [
        ProviderStreamEvent(type="reasoning_delta", reasoning_content=reasoning),
        ProviderStreamEvent(type="text_delta", text=text),
        ProviderStreamEvent(
            type="continuation",
            continuation=ProviderContinuationArtifact(
                provider_id="test",
                protocol="test",
                model_id="test",
                blocks=({"uncommitted_output": True},),
            ),
        ),
        ProviderStreamEvent(
            type="done",
            completion=ProviderCompletion(finish_reason=reason, provider_finish_reason=reason),
            usage=Usage(input_tokens=7, output_tokens=11),
        ),
    ]
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(events), ModelMetadata(model_id="test", provider_id="test")
        ),
        tools=ToolRegistry(),
    )
    try:
        result = await runtime.run("answer")
        snapshot = store.load_session(result.session.session_id)
        assert result.run.status == "failed"
        assert result.run.model_round_count == 1
        assert result.final_text == text
        assert result.run.usage["output_tokens"] == 11
        assert snapshot.events[-1].payload["error_code"] == code
        completed = next(e for e in snapshot.events if e.event_type == "model_message_completed")
        assert completed.payload["finish_reason"] == reason
        assert completed.payload["provider_finish_reason"] == reason
        assert completed.payload["usage"]["output_tokens"] == 11
        assert not any(e.event_type == "run_completed" for e in snapshot.events)
        assert not any(e.event_type == "run_retry_scheduled" for e in snapshot.events)
        if text or reasoning:
            assistant = snapshot.messages[-1]
            assert assistant.role == "assistant"
            assert assistant.text_content == text
            assert assistant.reasoning_content == reasoning
            assert assistant.metadata["incomplete"] is True
            assert assistant.provider_continuation is None
        else:
            assert [m.role for m in snapshot.messages] == ["user"]
    finally:
        await runtime.close()


@pytest.mark.parametrize("reason", ["length", "content_filter", "other"])
@pytest.mark.parametrize(
    "arguments", ['{"path":"unexpected.txt","content":"side effect"}', '{"path":']
)
async def test_unsuccessful_completion_never_executes_tool_fragments(
    tmp_path: Path, reason: ProviderFinishReason, arguments: str
) -> None:
    store = LocalSessionStore(tmp_path / "home")
    store.initialize()
    tools = ToolRegistry()
    for tool in builtin_tools():
        tools.register(tool)
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(type="text_delta", text="preamble"),
                    ProviderStreamEvent(
                        type="tool_call_delta",
                        tool_call_delta=ToolCallDelta(
                            index=0,
                            id_delta="call_1",
                            name_delta="Write",
                            arguments_delta=arguments,
                        ),
                    ),
                    ProviderStreamEvent(
                        type="done", completion=ProviderCompletion(finish_reason=reason)
                    ),
                ]
            ),
            ModelMetadata(model_id="test", provider_id="test"),
        ),
        tools=tools,
        default_workspace=tmp_path,
    )
    try:
        result = await runtime.run("write")
        assert result.run.status == "failed"
        assert result.events[-1].payload["error_code"] in {
            "model_output_limit",
            "model_content_filtered",
            "model_incomplete",
        }
        assert not (tmp_path / "unexpected.txt").exists()
        assert not result.tool_results
        assert result.run.active_tool_batch is None
        assert not any(e.event_type == "tool_call_started" for e in result.events)
        assert not any(e.event_type == "tool_call_requested" for e in result.events)
        assert any(e.event_type == "model_message_completed" for e in result.events)
    finally:
        await runtime.close()


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    ("reason", "message", "code"),
    [
        ("length", {"content": "partial"}, "model_output_limit"),
        ("stop", {"reasoning_content": "hidden thinking"}, "model_reasoning_only"),
        ("content_filter", {}, "model_content_filtered"),
        ("stop", {}, "model_empty_response"),
    ],
)
async def test_wire_completion_reaches_runtime_without_losing_reason_or_usage(
    tmp_path, streaming, reason, message, code
) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = {
            "choices": [{"delta" if streaming else "message": message, "finish_reason": reason}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 8},
        }
        if streaming:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text='data: {"choices":[{"delta":{}}]}\n\n'
                + "data: "
                + json.dumps(body)
                + "\n\ndata: [DONE]\n\n",
            )
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIChatCompletionsProvider(
            base_url="https://example.test",
            transport=HttpxProviderHTTPTransport(client=client),
        )
        store = LocalSessionStore(tmp_path)
        store.initialize()
        runtime = RuntimeService(
            store=store,
            adapter=RuntimeAdapter(
                provider,
                ModelMetadata(model_id="test", provider_id="test", supports_streaming=streaming),
            ),
            tools=ToolRegistry(),
        )
        try:
            result = await runtime.run("answer")
            assert result.run.status == "failed"
            assert result.final_text == message.get("content", "")
            assert result.events[-1].payload["error_code"] == code
            completed = next(e for e in result.events if e.event_type == "model_message_completed")
            assert completed.payload["finish_reason"] == reason
            assert completed.payload["usage"]["output_tokens"] == 8
            assert completed.payload["has_reasoning"] == bool(message.get("reasoning_content"))
            assert result.run.usage["output_tokens"] == 8
            assert len(requests) == 1
        finally:
            await runtime.close()
