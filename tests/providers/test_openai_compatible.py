from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
import pytest

from ouro.core.messages import ImageContent, MessageRecord, TextContent, ToolResultReference
from ouro.core.provider import ModelMetadata
from ouro.core.tools import ToolCallRequest
from ouro.providers import OpenAICompatibleProvider
from ouro.providers.openai_compatible import HttpxOpenAICompatibleTransport


class FakeTransport:
    def __init__(self, response: dict[str, Any] | Exception) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    async def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
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


class FakeStreamingTransport:
    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self.chunks = chunks
        self.requests: list[dict[str, Any]] = []

    async def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        del url, headers, payload, timeout
        raise AssertionError("streaming transport should not use post_json")

    async def stream_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> AsyncIterator[Mapping[str, Any]]:
        self.requests.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout": timeout,
            }
        )
        for chunk in self.chunks:
            yield chunk


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
async def test_httpx_transport_streams_sse_json() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"he"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
            "data: [DONE]\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = HttpxOpenAICompatibleTransport(client)
        chunks = [
            chunk
            async for chunk in transport.stream_json(
                url="https://example.test/v1/chat/completions",
                headers={"authorization": "Bearer secret"},
                payload={"model": "glm-test", "stream": True},
                timeout=12,
            )
        ]

    assert [chunk["choices"][0]["delta"]["content"] for chunk in chunks] == ["he", "llo"]
    assert requests[0].headers["accept"] == "text/event-stream"
    assert json.loads(requests[0].content)["stream"] is True


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
async def test_openai_endpoint_options_and_system_prompt_shape_request() -> None:
    transport = FakeTransport({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        endpoint="responses/chat",
        request_options={
            "seed": 7,
            "model": "wrong-model",
            "messages": [],
            "stream": True,
            "temperature": 1.9,
            "max_tokens": 999,
            "reasoning_effort": "low",
            "tools": [{"type": "wrong"}],
        },
        transport=transport,
    )
    model = ModelMetadata(
        model_id="reasoning-model",
        provider_id="glm",
        endpoint_id="reasoning",
        temperature=0.2,
        max_output_tokens=4096,
        supports_streaming=False,
        supports_reasoning=True,
        supported_thinking_levels=("low", "high"),
        default_thinking_level="high",
    )

    events = [
        event
        async for event in provider.stream(
            model=model,
            system_prompt="System instructions.",
            messages=[MessageRecord.text(session_id="ses_1", role="user", text="hello")],
        )
    ]

    assert [event.type for event in events] == ["text_delta", "done"]
    request = transport.requests[0]
    assert request["url"] == "https://example.test/v1/responses/chat"
    assert request["payload"]["messages"] == [
        {"role": "system", "content": "System instructions."},
        {"role": "user", "content": "hello"},
    ]
    assert request["payload"]["stream"] is False
    assert request["payload"]["model"] == "reasoning-model"
    assert request["payload"]["temperature"] == 0.2
    assert request["payload"]["max_tokens"] == 4096
    assert request["payload"]["reasoning_effort"] == "high"
    assert request["payload"]["seed"] == 7
    assert "tools" not in request["payload"]


@pytest.mark.asyncio
async def test_openai_endpoint_capabilities_gate_tools_and_encode_images() -> None:
    transport = FakeTransport({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        transport=transport,
    )
    message = MessageRecord(
        session_id="ses_1",
        role="user",
        content=[
            TextContent(text="describe"),
            ImageContent(uri="data:image/png;base64,abc", media_type="image/png"),
        ],
    )

    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(
                model_id="vision-model",
                provider_id="glm",
                supports_streaming=False,
                supports_tools=False,
                supports_vision=True,
            ),
            messages=[message],
            tools=[{"name": "Read", "parameters_schema": {"type": "object"}}],
        )
    ]

    assert [event.type for event in events] == ["text_delta", "done"]
    payload = transport.requests[0]["payload"]
    assert "tools" not in payload
    assert payload["messages"][0]["content"] == [
        {"type": "text", "text": "describe"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,abc"},
        },
    ]


@pytest.mark.asyncio
async def test_openai_endpoint_rejects_images_when_multimodal_is_disabled() -> None:
    transport = FakeTransport({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        transport=transport,
    )
    message = MessageRecord(
        session_id="ses_1",
        role="user",
        content=[ImageContent(uri="data:image/png;base64,abc", media_type="image/png")],
    )

    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(
                model_id="text-model",
                provider_id="glm",
                supports_vision=False,
            ),
            messages=[message],
        )
    ]

    assert [event.type for event in events] == ["error"]
    assert events[0].error == "model endpoint does not support image input"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_openai_endpoint_rejects_unexpected_tool_calls_when_tools_are_disabled() -> None:
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
                                "function": {"name": "Read", "arguments": "{}"},
                            }
                        ],
                    }
                }
            ]
        }
    )
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        transport=transport,
    )

    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(
                model_id="text-model",
                provider_id="glm",
                supports_tools=False,
                supports_streaming=False,
            ),
            messages=[MessageRecord.text(session_id="ses_1", role="user", text="hello")],
        )
    ]

    assert [event.type for event in events] == ["error"]
    assert "without tool support" in (events[0].error or "")


@pytest.mark.asyncio
async def test_openai_compatible_provider_streams_text_chunks() -> None:
    transport = FakeStreamingTransport(
        [
            {"choices": [{"delta": {"content": "hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
            {"usage": {"prompt_tokens": 3, "completion_tokens": 2}, "choices": []},
        ]
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
            messages=[MessageRecord.text(session_id="ses_1", role="user", text="say hello")],
        )
    ]

    assert [event.type for event in events] == ["text_delta", "text_delta", "done"]
    assert [events[0].text, events[1].text] == ["hel", "lo"]
    assert events[-1].usage is not None
    assert events[-1].usage.input_tokens == 3
    assert transport.requests[0]["payload"]["stream"] is True


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
                            },
                            {
                                "id": "call_2",
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "arguments": '{"path": "CHANGELOG.md"}',
                                },
                            },
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

    assert [event.type for event in events] == ["done"]
    assert [call.model_dump() for call in events[0].tool_calls] == [
        {
            "id": "call_1",
            "name": "Read",
            "arguments": {"path": "README.md"},
            "source_index": 0,
        },
        {
            "id": "call_2",
            "name": "Read",
            "arguments": {"path": "CHANGELOG.md"},
            "source_index": 1,
        },
    ]
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
async def test_openai_compatible_provider_streams_tool_call_chunks() -> None:
    transport = FakeStreamingTransport(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "Read", "arguments": '{"path"'},
                                },
                                {
                                    "index": 1,
                                    "id": "call_2",
                                    "type": "function",
                                    "function": {
                                        "name": "Read",
                                        "arguments": '{"path": "CHANGELOG.md"}',
                                    },
                                },
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": ': "README.md"}'},
                                }
                            ]
                        }
                    }
                ]
            },
        ]
    )
    provider = OpenAICompatibleProvider(base_url="http://localhost:8000/v1", transport=transport)

    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(model_id="local", provider_id="local"),
            messages=[MessageRecord.text(session_id="ses_1", role="user", text="read")],
        )
    ]

    assert [event.type for event in events] == ["done"]
    assert [call.model_dump() for call in events[0].tool_calls] == [
        {
            "id": "call_1",
            "name": "Read",
            "arguments": {"path": "README.md"},
            "source_index": 0,
        },
        {
            "id": "call_2",
            "name": "Read",
            "arguments": {"path": "CHANGELOG.md"},
            "source_index": 1,
        },
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
            "tool_calls": [
                ToolCallRequest(
                    id="call_1",
                    name="Read",
                    arguments={"path": "README.md"},
                )
            ]
        }
    )
    tool_result = MessageRecord.text(
        session_id="ses_1",
        role="tool_result",
        text="contents",
    ).model_copy(
        update={"tool_result": ToolResultReference(tool_call_id="call_1", tool_name="Read")}
    )

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


@pytest.mark.asyncio
async def test_openai_compatible_provider_emits_reasoning_delta_from_response() -> None:
    transport = FakeTransport(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "The answer is 4.",
                        "reasoning_content": "Let me compute 2 + 2.",
                    }
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 4},
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
            messages=[MessageRecord.text(session_id="ses_1", role="user", text="2+2")],
        )
    ]

    assert [event.type for event in events] == [
        "reasoning_delta",
        "text_delta",
        "done",
    ]
    assert events[0].reasoning_content == "Let me compute 2 + 2."
    assert events[1].text == "The answer is 4."


@pytest.mark.asyncio
async def test_openai_compatible_provider_emits_reasoning_delta_from_stream_chunks() -> None:
    transport = FakeStreamingTransport(
        [
            {"choices": [{"delta": {"reasoning_content": "Thinking"}}]},
            {"choices": [{"delta": {"reasoning_content": "..."}}]},
            {"choices": [{"delta": {"content": "Done"}}]},
            {"usage": {"prompt_tokens": 3, "completion_tokens": 3}, "choices": []},
        ]
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
            messages=[MessageRecord.text(session_id="ses_1", role="user", text="go")],
        )
    ]

    assert [event.type for event in events] == [
        "reasoning_delta",
        "reasoning_delta",
        "text_delta",
        "done",
    ]
    assert [event.reasoning_content for event in events[:2]] == ["Thinking", "..."]
    assert events[2].text == "Done"


@pytest.mark.asyncio
async def test_openai_compatible_provider_reasoning_only_response_is_not_empty() -> None:
    transport = FakeTransport(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "Only reasoning, no content.",
                    }
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5},
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
            messages=[MessageRecord.text(session_id="ses_1", role="user", text="think")],
        )
    ]

    assert [event.type for event in events] == ["reasoning_delta", "done"]
    assert events[0].reasoning_content == "Only reasoning, no content."


@pytest.mark.asyncio
async def test_openai_compatible_provider_sends_reasoning_content_in_history() -> None:
    transport = FakeTransport({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="secret",
        transport=transport,
    )
    assistant = MessageRecord.text(session_id="ses_1", role="assistant", text="").model_copy(
        update={
            "tool_calls": [
                ToolCallRequest(
                    id="call_1",
                    name="Read",
                    arguments={"path": "README.md"},
                )
            ],
            "metadata": {"reasoning_content": "I need to read the file."},
        }
    )
    tool_result = MessageRecord.text(
        session_id="ses_1", role="tool_result", text="contents"
    ).model_copy(
        update={"tool_result": ToolResultReference(tool_call_id="call_1", tool_name="Read")}
    )

    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(model_id="glm-test", provider_id="glm"),
            messages=[
                MessageRecord.text(session_id="ses_1", role="user", text="read"),
                assistant,
                tool_result,
            ],
        )
    ]

    assert [event.type for event in events] == ["text_delta", "done"]
    assistant_message = transport.requests[0]["payload"]["messages"][1]
    assert assistant_message["role"] == "assistant"
    assert assistant_message["reasoning_content"] == "I need to read the file."
    assert assistant_message["tool_calls"][0]["id"] == "call_1"


@pytest.mark.asyncio
async def test_openai_compatible_provider_sends_empty_reasoning_content_for_tool_calls() -> None:
    transport = FakeTransport({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1",
        api_key="secret",
        transport=transport,
    )
    assistant = MessageRecord.text(session_id="ses_1", role="assistant", text="").model_copy(
        update={
            "metadata": {
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "Read",
                        "arguments": {"path": "README.md"},
                    }
                ],
                "reasoning_content": "",
            }
        }
    )

    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(model_id="glm-test", provider_id="glm"),
            messages=[
                MessageRecord.text(session_id="ses_1", role="user", text="read"),
                assistant,
            ],
        )
    ]

    assert [event.type for event in events] == ["text_delta", "done"]
    assistant_message = transport.requests[0]["payload"]["messages"][1]
    assert assistant_message["role"] == "assistant"
    assert "reasoning_content" in assistant_message
    assert assistant_message["reasoning_content"] == ""
