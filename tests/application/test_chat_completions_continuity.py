from __future__ import annotations

import json

import httpx
import pytest

from ouro.application.runtime.service import RuntimeService
from ouro.config.reasoning import mapped_reasoning
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata
from ouro.core.reasoning import ReasoningRequest
from ouro.providers import OpenAIChatCompletionsProvider
from ouro.providers.http_transport import HttpxProviderHTTPTransport
from ouro.providers.reasoning import ReasoningPayloadEncoder
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult
from tests.application.test_session_models import _StaticCatalog


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("text_field", ["reasoning_content", "reasoning"])
@pytest.mark.parametrize("resume_tools", [False, True])
async def test_model_switch_after_restart_preserves_reasoning_and_completed_tools(
    tmp_path, backend, streaming, text_field, resume_tools
):
    def open_store():
        return (
            LocalSessionStore(tmp_path)
            if backend == "files"
            else SQLiteSessionStore(tmp_path / "sessions.db")
        )

    store = open_store()
    store.initialize()
    requests = []
    executions = []
    details = [
        {"type": "reasoning.text", "text": "Inspect first.", "signature": "source-signature"}
    ]

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["model"] == "source-model" and len(requests) == 1:
            message = {
                "reasoning": "Inspect first.",
                "reasoning_details": details,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "Inspect", "arguments": "{}"},
                    }
                ],
            }
            finish = "tool_calls"
        elif payload["model"] == "source-model":
            if resume_tools:
                return httpx.Response(400, json={"error": {"message": "source unavailable"}})
            message, finish = {"content": "Inspected."}, "stop"
        else:
            assistants = [m for m in payload["messages"] if m["role"] == "assistant"]
            assert assistants[0][text_field] == "Inspect first."
            assert len(assistants) == (1 if resume_tools else 2)
            if not resume_tools:
                assert text_field not in assistants[1]
            assert all("reasoning_details" not in m for m in assistants)
            other_field = "reasoning" if text_field == "reasoning_content" else "reasoning_content"
            assert all(other_field not in m for m in assistants)
            assert [m["tool_call_id"] for m in payload["messages"] if m["role"] == "tool"] == [
                "call_1"
            ]
            assert payload["thinking"] == {"type": "enabled"}
            assert payload["tools"]
            message, finish = (
                {text_field: "Continue from the facts.", "content": "Continued."},
                "stop",
            )
        choice = {"delta" if streaming else "message": message, "finish_reason": finish}
        body = {"choices": [choice]}
        if streaming:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n",
            )
        return httpx.Response(200, json=body)

    reasoning = mapped_reasoning(
        name="thinking",
        activation="toggle",
        effort={
            "enabled": {"thinking": {"type": "enabled"}},
            "disabled": {"thinking": {"type": "disabled"}},
        },
        default=ReasoningRequest(mode="enabled"),
    )
    tools = ToolRegistry()

    def inspect(invocation, context):
        executions.append(invocation.tool_call_id)
        return ToolResult(
            tool_call_id=invocation.tool_call_id, tool_name="Inspect", content="file facts"
        )

    tools.register(
        Tool(
            definition=ToolDefinition(name="Inspect", description="Inspect a file."),
            handler=inspect,
        )
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapters = {}
        for identity in ("source", "target"):
            provider = OpenAIChatCompletionsProvider(
                base_url="https://example.test",
                provider_id=identity,
                reasoning_text_field=text_field if identity == "target" else None,
                transport=HttpxProviderHTTPTransport(client=client),
                reasoning_encoder=ReasoningPayloadEncoder(
                    reasoning.profile if identity == "target" else None
                ),
            )
            adapters[f"{identity}/default"] = RuntimeAdapter(
                provider,
                ModelMetadata(
                    provider_id=identity,
                    endpoint_id="default",
                    model_id=f"{identity}-model",
                    supports_streaming=streaming,
                    reasoning=reasoning.capability,
                ),
            )
        catalog = _StaticCatalog(adapters, "source/default")
        runtime = RuntimeService(
            store=store, adapter=adapters["source/default"], model_catalog=catalog, tools=tools
        )
        try:
            first = await runtime.run("Inspect the file.")
            assert first.run.status == ("failed" if resume_tools else "completed"), first.run.error
            session_id = first.session.session_id
            before = store.load_session(session_id).messages
            runtime.switch_session_model(session_id, "target/default")
        finally:
            await runtime.close()
        store = open_store()
        store.initialize()
        runtime = RuntimeService(
            store=store, adapter=adapters["source/default"], model_catalog=catalog, tools=tools
        )
        try:
            result = (
                await runtime.continue_run(session_id=session_id)
                if resume_tools
                else await runtime.run("Continue.", session_id=session_id)
            )
            assert result.run.status == "completed", result.run.error
            assert result.final_text == "Continued."
            assert executions == ["call_1"]
            assert len(requests) == 3
            after = store.load_session(session_id).messages
            assert after[: len(before)] == before
            assert after[-1].reasoning_content == "Continue from the facts."
            context = store.load_latest_context(session_id)
            assistants = [m for m in context.provider_context.messages if m.role == "assistant"]
            assert [m.reasoning_content for m in assistants] == (
                ["Inspect first."] if resume_tools else ["Inspect first.", ""]
            )
            assert all(m.provider_continuation is None for m in assistants)
        finally:
            await runtime.close()
