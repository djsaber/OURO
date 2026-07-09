from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ouro.core.messages import MessageRecord
from ouro.providers import ModelMetadata, OpenAICompatibleProvider
from ouro.providers.openai_compatible import HttpxOpenAICompatibleTransport


class FakeTransport:
    def __init__(self, response: dict[str, Any] | Exception) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    async def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        self.requests.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout": timeout,
            }
        )
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.mark.asyncio
async def test_httpx_transport_posts_json() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = HttpxOpenAICompatibleTransport(client)
        result = await transport.post_json(
            url="https://example.test/v1/chat/completions",
            headers={"authorization": "Bearer secret"},
            payload={"model": "glm-test"},
            timeout=12,
        )

    assert result == {"ok": True}
    assert requests[0].headers["authorization"] == "Bearer secret"
    assert json.loads(requests[0].content) == {"model": "glm-test"}


@pytest.mark.asyncio
async def test_httpx_transport_reports_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(429, text="too many requests")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = HttpxOpenAICompatibleTransport(client)
        with pytest.raises(RuntimeError, match="provider HTTP 429"):
            await transport.post_json(
                url="https://example.test/v1/chat/completions",
                headers={},
                payload={"model": "glm-test"},
                timeout=12,
            )


@pytest.mark.asyncio
async def test_openai_compatible_provider_streams_text_response() -> None:
    transport = FakeTransport(
        {
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }
    )
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="secret",
        transport=transport,
    )

    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(model_id="glm-test", provider_id="glm"),
            messages=[
                MessageRecord.text(session_id="ses_1", role="user", text="say hello"),
            ],
        )
    ]

    assert [event.type for event in events] == ["text_delta", "done"]
    assert events[0].text == "hello"
    assert events[-1].usage is not None
    assert events[-1].usage.input_tokens == 3
    assert events[-1].usage.output_tokens == 2
    assert transport.requests[0]["url"] == "https://example.test/v1/chat/completions"
    assert transport.requests[0]["headers"]["authorization"] == "Bearer secret"
    assert transport.requests[0]["payload"]["messages"] == [
        {"role": "user", "content": "say hello"}
    ]


@pytest.mark.asyncio
async def test_openai_compatible_provider_maps_tool_calls_and_schemas() -> None:
    transport = FakeTransport(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "arguments": '{"path": "README.md"}',
                                },
                            }
                        ],
                    }
                }
            ]
        }
    )
    provider = OpenAICompatibleProvider(base_url="http://localhost:8000/v1", transport=transport)

    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(model_id="local", provider_id="local"),
            messages=[MessageRecord.text(session_id="ses_1", role="user", text="read")],
            tools=[
                {
                    "name": "Read",
                    "description": "Read a file.",
                    "parameters_schema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                }
            ],
        )
    ]

    assert [event.type for event in events] == ["tool_call", "done"]
    assert events[0].tool_call == {
        "id": "call_1",
        "name": "Read",
        "arguments": {"path": "README.md"},
    }
    assert transport.requests[0]["payload"]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "Read",
                "description": "Read a file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        }
    ]


@pytest.mark.asyncio
async def test_openai_compatible_provider_reports_empty_assistant_message() -> None:
    transport = FakeTransport(
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": ""},
                    "finish_reason": "stop",
                }
            ]
        }
    )
    provider = OpenAICompatibleProvider(base_url="https://example.test/v1", transport=transport)

    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(model_id="glm-test", provider_id="glm"),
            messages=[
                MessageRecord.text(session_id="ses_1", role="user", text="say hello"),
            ],
        )
    ]

    assert [event.type for event in events] == ["error"]
    assert events[0].error == "provider returned an empty assistant message finish_reason=stop"


@pytest.mark.asyncio
async def test_openai_compatible_provider_reports_error_response_body() -> None:
    transport = FakeTransport(
        {
            "error": {
                "message": "unknown model",
                "code": "model_not_found",
            }
        }
    )
    provider = OpenAICompatibleProvider(base_url="https://example.test/v1", transport=transport)

    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(model_id="missing-model", provider_id="glm"),
            messages=[
                MessageRecord.text(session_id="ses_1", role="user", text="say hello"),
            ],
        )
    ]

    assert [event.type for event in events] == ["error"]
    assert events[0].error == "unknown model (model_not_found)"


@pytest.mark.asyncio
async def test_openai_compatible_provider_sends_assistant_tool_call_history() -> None:
    transport = FakeTransport({"choices": [{"message": {"role": "assistant", "content": "done"}}]})
    provider = OpenAICompatibleProvider(base_url="http://localhost:8000/v1", transport=transport)
    assistant = MessageRecord.text(session_id="ses_1", role="assistant", text="").model_copy(
        update={
            "metadata": {
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "Read",
                        "arguments": {"path": "README.md"},
                    }
                ]
            }
        }
    )
    tool_result = MessageRecord.text(
        session_id="ses_1",
        role="tool_result",
        text="contents",
    ).model_copy(update={"metadata": {"tool_call_id": "call_1"}})

    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(model_id="local", provider_id="local"),
            messages=[
                MessageRecord.text(session_id="ses_1", role="user", text="read"),
                assistant,
                tool_result,
            ],
        )
    ]

    assert [event.type for event in events] == ["text_delta", "done"]
    assert transport.requests[0]["payload"]["messages"] == [
        {"role": "user", "content": "read"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": '{"path": "README.md"}',
                    },
                }
            ],
        },
        {"role": "tool", "content": "contents", "tool_call_id": "call_1"},
    ]


@pytest.mark.asyncio
async def test_openai_compatible_provider_connection_check_reports_status() -> None:
    success_transport = FakeTransport({"choices": [{"message": {"content": "ok"}}]})
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        provider_id="glm",
        transport=success_transport,
    )

    success = await provider.check_connection(ModelMetadata(model_id="glm-test", provider_id="glm"))

    assert success.ok is True
    assert success.provider_id == "glm"
    assert success.model_id == "glm-test"

    failed_transport = FakeTransport(RuntimeError("network down"))
    failed_provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        provider_id="glm",
        transport=failed_transport,
    )

    failed = await failed_provider.check_connection(
        ModelMetadata(model_id="glm-test", provider_id="glm")
    )

    assert failed.ok is False
    assert failed.message == "network down"

    empty_transport = FakeTransport(
        {
            "choices": [
                {
                    "message": {"content": ""},
                    "finish_reason": "stop",
                }
            ]
        }
    )
    empty_provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        provider_id="glm",
        transport=empty_transport,
    )

    empty = await empty_provider.check_connection(
        ModelMetadata(model_id="glm-test", provider_id="glm")
    )

    assert empty.ok is False
    assert empty.message == "provider returned an empty assistant message finish_reason=stop"
