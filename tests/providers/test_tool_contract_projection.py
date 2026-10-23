import json

import httpx
import pytest

from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata
from ouro.providers import (
    AnthropicMessagesProvider,
    OpenAIChatCompletionsProvider,
    OpenAIResponsesProvider,
)
from ouro.providers.http_transport import HttpxProviderHTTPTransport
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry
from tests.providers.responses_fixtures import message, terminal


@pytest.mark.parametrize("backend", ["file", "sqlite"])
@pytest.mark.parametrize("protocol", ["responses", "chat_completions", "messages"])
async def test_protocols_send_and_persist_the_same_host_tool_contract(tmp_path, backend, protocol):
    requests = []
    responses = {
        "responses": terminal([message("done")])["response"],
        "chat_completions": {
            "choices": [
                {"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}
            ]
        },
        "messages": {
            "type": "message",
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
        },
    }

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[protocol])

    def unexpected_execution(invocation, context):
        raise AssertionError("the model did not request tool execution")

    registry = ToolRegistry()
    for source in ("builtin", "third_party_plugin", "mcp"):
        registry.register(
            Tool(
                definition=ToolDefinition(
                    name=f"Boundary_{source}",
                    description="Original description.",
                    parameters_schema={"type": "object", "properties": {}},
                    source=source,
                    batch_behavior="exclusive",
                ),
                handler=unexpected_execution,
            )
        )
    originals = [definition.model_dump() for definition in registry.definitions()]
    store = (
        LocalSessionStore(tmp_path / "sessions")
        if backend == "file"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    provider_type = {
        "responses": OpenAIResponsesProvider,
        "chat_completions": OpenAIChatCompletionsProvider,
        "messages": AnthropicMessagesProvider,
    }[protocol]

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        runtime = RuntimeService(
            store=store,
            adapter=RuntimeAdapter(
                provider_type(
                    base_url="https://provider.invalid",
                    transport=HttpxProviderHTTPTransport(client),
                ),
                ModelMetadata(
                    max_output_tokens=8192,
                    model_id="test",
                    provider_id="test",
                    supports_streaming=False,
                ),
            ),
            tools=registry,
        )
        result = await runtime.run("Describe the available tools.")

    assert result.run.status == "completed"
    assert len(requests) == 1
    wire_tools = requests[0]["tools"]
    if protocol == "chat_completions":
        wire_tools = [tool["function"] for tool in wire_tools]
    schema_key = "input_schema" if protocol == "messages" else "parameters"
    description = (
        "Original description.\n\n"
        "This must be the only tool call in your response. If you include other tool calls, "
        "none of them will execute."
    )
    assert [tool["name"] for tool in wire_tools] == [tool["name"] for tool in originals]
    assert all(tool["description"] == description for tool in wire_tools)
    assert all(tool[schema_key] == originals[0]["parameters_schema"] for tool in wire_tools)
    assert [definition.model_dump() for definition in registry.definitions()] == originals

    saved = runtime.sessions.context(result.session.session_id)
    assert saved is not None
    assert all(tool["description"] == description for tool in saved.provider_context.tools)
    assert runtime.sessions.context(result.session.session_id) == saved
