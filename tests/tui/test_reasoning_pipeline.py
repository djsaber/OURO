import json
from pathlib import Path

import httpx
import pytest

from ouro.application.runtime import RuntimeService
from ouro.config.reasoning import reasoning_effort_preset
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata
from ouro.core.reasoning import ReasoningRequest
from ouro.providers import OpenAIChatCompletionsProvider
from ouro.providers.http_transport import HttpxProviderHTTPTransport
from ouro.providers.reasoning import ReasoningPayloadEncoder
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from ouro.tui.widgets import _project_transcript_entries


@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
@pytest.mark.parametrize("streaming", [False, True])
async def test_max_effort_response_reaches_persisted_conversation_history(
    tmp_path: Path,
    store_type: type[LocalSessionStore] | type[SQLiteSessionStore],
    streaming: bool,
) -> None:
    reasoning = reasoning_effort_preset("openai-chat-completions-effort")
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        body = {
            "choices": [
                {
                    "delta" if streaming else "message": {
                        "role": "assistant",
                        "reasoning_content": "Checking the evidence.",
                        "content": "The answer.",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        if streaming:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n",
            )
        return httpx.Response(200, json=body)

    store = store_type(
        tmp_path / "sessions.sqlite3" if store_type is SQLiteSessionStore else tmp_path
    )
    store.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIChatCompletionsProvider(
            base_url="https://example.test",
            provider_id="test",
            transport=HttpxProviderHTTPTransport(client=client),
            reasoning_encoder=ReasoningPayloadEncoder(reasoning.profile),
        )
        runtime = RuntimeService(
            store=store,
            adapter=RuntimeAdapter(
                provider,
                ModelMetadata(
                    model_id="test-model",
                    provider_id="test",
                    supports_streaming=streaming,
                    reasoning=reasoning.capability,
                ),
            ),
            tools=ToolRegistry(),
        )
        try:
            result = await runtime.run(
                "answer", initial_reasoning_request=ReasoningRequest(effort="max")
            )
            assert result.run.status == "completed"
            assert len(requests) == 1
            assert requests[0]["reasoning_effort"] == "max"
            snapshot = store.load_session(result.session.session_id)
            assistant = snapshot.messages[-1]
            assert assistant.reasoning_content == "Checking the evidence."
            assert assistant.provider_continuation is not None
            assert any(e.event_type == "model_reasoning_delta" for e in snapshot.events)
            entries, _ = _project_transcript_entries(
                snapshot.messages,
                (),
                (),
                mutable_tool_call_keys=frozenset(),
                visible_scaffold_delegation_id=None,
            )
            assert [entry.reasoning for entry in entries if entry.reasoning] == [
                "Checking the evidence."
            ]
            assert entries[-1].text == "The answer."
        finally:
            await runtime.close()
