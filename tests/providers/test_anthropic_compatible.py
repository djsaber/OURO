from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest

from ouro.config import get_provider_template
from ouro.core.messages import (
    ImageContent,
    MessageRecord,
    ProviderContinuationArtifact,
    TextContent,
    ToolResultReference,
)
from ouro.core.provider import ModelMetadata
from ouro.core.reasoning import ReasoningRequest
from ouro.core.tools import ToolCallRequest
from ouro.providers import AnthropicCompatibleProvider
from ouro.providers.reasoning import ReasoningPayloadEncoder


class FakeTransport:
    def __init__(self, response: dict[str, Any]) -> None:
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
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout": timeout,
            }
        )
        return self.response


class FakeStreamingTransport(FakeTransport):
    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        super().__init__({})
        self.chunks = chunks

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
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout": timeout,
            }
        )
        for chunk in self.chunks:
            yield chunk


@pytest.mark.asyncio
async def test_anthropic_provider_maps_messages_tools_and_response_blocks() -> None:
    transport = FakeTransport(
        {
            "type": "message",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "I should inspect the file.",
                    "signature": "sig_1",
                },
                {"type": "text", "text": "I'll read it."},
                {
                    "type": "tool_use",
                    "id": "call_2",
                    "name": "Read",
                    "input": {"path": "CHANGELOG.md"},
                },
            ],
            "usage": {
                "input_tokens": 12,
                "output_tokens": 7,
                "cache_read_input_tokens": 3,
            },
        }
    )
    reasoning = get_provider_template("anthropic").endpoint().reasoning
    provider = AnthropicCompatibleProvider(
        base_url="https://api.example.test",
        api_key="secret",
        transport=transport,
        reasoning_encoder=ReasoningPayloadEncoder(reasoning.profile),
    )
    assistant = MessageRecord.text(
        session_id="ses_1", role="assistant", text="I'll inspect it."
    ).model_copy(
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
        session_id="ses_1", role="tool_result", text="contents"
    ).model_copy(
        update={
            "tool_result": ToolResultReference(
                tool_call_id="call_1",
                tool_name="Read",
            )
        }
    )
    user = MessageRecord(
        session_id="ses_1",
        role="user",
        content=[
            TextContent(text="Read this image too."),
            ImageContent(uri="data:image/png;base64,abc", media_type="image/png"),
        ],
    )
    model = ModelMetadata(
        model_id="claude-test",
        provider_id="anthropic",
        supports_streaming=False,
        supports_vision=True,
        reasoning=reasoning.capability,
        reasoning_request=ReasoningRequest(mode="enabled", effort="low"),
    )

    events = [
        event
        async for event in provider.stream(
            model=model,
            system_prompt="Be precise.",
            messages=[
                MessageRecord.text(
                    session_id="ses_1",
                    role="system_synthetic",
                    text="Workspace policy.",
                ),
                user,
                assistant,
                tool_result,
            ],
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

    assert [event.type for event in events] == [
        "reasoning_delta",
        "text_delta",
        "tool_call_delta",
        "continuation",
        "done",
    ]
    assert events[2].tool_call_delta is not None
    assert events[2].tool_call_delta.arguments_delta == '{"path":"CHANGELOG.md"}'
    assert events[3].continuation is not None
    assert events[3].continuation.blocks[0]["signature"] == "sig_1"
    assert events[-1].usage is not None
    assert events[-1].usage.cache_read_tokens == 3
    request = transport.requests[0]
    assert request["url"] == "https://api.example.test/v1/messages"
    assert request["headers"] == {
        "anthropic-version": "2023-06-01",
        "x-api-key": "secret",
    }
    assert request["payload"]["system"] == (
        "Be precise.\n\n<system-reminder>\nWorkspace policy.\n</system-reminder>"
    )
    assert request["payload"]["output_config"] == {"effort": "low"}
    assert request["payload"]["messages"][0]["content"][1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "abc",
        },
    }
    assert request["payload"]["messages"][1]["content"][1] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "Read",
        "input": {"path": "README.md"},
    }
    assert request["payload"]["messages"][2]["content"][0]["type"] == "tool_result"
    assert request["payload"]["tools"][0]["input_schema"]["type"] == "object"


@pytest.mark.asyncio
async def test_anthropic_provider_streams_text_tool_deltas_and_usage() -> None:
    transport = FakeStreamingTransport(
        [
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 5, "output_tokens": 0}},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hello"},
            },
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "Read",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"path":"README.md"}',
                },
            },
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {
                    "type": "tool_use",
                    "id": "call_2",
                    "name": "Bash",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"command":"pwd"}',
                },
            },
            {"type": "message_delta", "usage": {"output_tokens": 4}},
            {"type": "message_stop"},
        ]
    )
    provider = AnthropicCompatibleProvider(
        base_url="https://api.example.test",
        transport=transport,
    )

    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(model_id="claude-test", provider_id="anthropic"),
            messages=[MessageRecord.text(session_id="ses_1", role="user", text="hello")],
        )
    ]

    assert [event.type for event in events] == [
        "text_delta",
        "tool_call_delta",
        "tool_call_delta",
        "tool_call_delta",
        "tool_call_delta",
        "done",
    ]
    deltas = [event.tool_call_delta for event in events if event.tool_call_delta is not None]
    assert [delta.index for delta in deltas] == [0, 0, 1, 1]
    assert deltas[0].id_delta == "call_1"
    assert deltas[1].arguments_delta == '{"path":"README.md"}'
    assert deltas[2].id_delta == "call_2"
    assert deltas[3].arguments_delta == '{"command":"pwd"}'
    assert events[-1].usage is not None
    assert events[-1].usage.input_tokens == 5
    assert events[-1].usage.output_tokens == 4


@pytest.mark.asyncio
async def test_anthropic_stream_rejects_tool_input_without_a_started_tool_block() -> None:
    transport = FakeStreamingTransport(
        [
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 1, "output_tokens": 0}},
            },
            {
                "type": "content_block_delta",
                "index": 3,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"path":"README.md"}',
                },
            },
            {"type": "message_stop"},
        ]
    )
    provider = AnthropicCompatibleProvider(
        base_url="https://api.example.test",
        transport=transport,
    )

    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(model_id="claude-test", provider_id="anthropic"),
            messages=[MessageRecord.text(session_id="ses_1", role="user", text="read")],
        )
    ]

    assert [event.type for event in events] == ["error"]
    assert events[0].error == ("provider streamed tool input for an unknown content block index: 3")


@pytest.mark.asyncio
async def test_anthropic_stream_preserves_signed_thinking_for_same_route_only() -> None:
    transport = FakeStreamingTransport(
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 2}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "check"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "sig"},
            },
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "Read",
                    "input": {},
                },
            },
            {"type": "message_stop"},
        ]
    )
    reasoning = get_provider_template("anthropic").endpoint("claude-opus-4-8").reasoning
    provider = AnthropicCompatibleProvider(
        base_url="https://api.example.test",
        provider_id="anthropic",
        transport=transport,
        reasoning_encoder=ReasoningPayloadEncoder(reasoning.profile),
    )
    model = ModelMetadata(
        model_id="claude-opus-4-8",
        provider_id="anthropic",
        reasoning=reasoning.capability,
        reasoning_request=ReasoningRequest(mode="enabled", effort="high"),
    )

    events = [
        event
        async for event in provider.stream(
            model=model,
            messages=[MessageRecord.text(session_id="ses_1", role="user", text="think")],
        )
    ]

    assert [event.type for event in events] == [
        "reasoning_delta",
        "tool_call_delta",
        "continuation",
        "done",
    ]
    continuation = events[2].continuation
    assert continuation is not None
    assert continuation.blocks == ({"type": "thinking", "thinking": "check", "signature": "sig"},)

    replay_transport = FakeTransport(
        {"type": "message", "content": [{"type": "text", "text": "ok"}]}
    )
    provider.transport = replay_transport
    matching = MessageRecord.text(
        session_id="ses_1", role="assistant", text="calling tool"
    ).model_copy(
        update={
            "provider_continuation": continuation,
            "tool_calls": [ToolCallRequest(id="call_1", name="Read", arguments={})],
        }
    )
    mismatched = matching.model_copy(
        update={
            "provider_continuation": ProviderContinuationArtifact(
                provider_id="anthropic",
                protocol="anthropic-messages",
                model_id="other-model",
                blocks=continuation.blocks,
            )
        }
    )

    async for _ in provider.stream(model=model, messages=[matching]):
        pass
    async for _ in provider.stream(model=model, messages=[mismatched]):
        pass

    matching_content = replay_transport.requests[0]["payload"]["messages"][0]["content"]
    mismatched_content = replay_transport.requests[1]["payload"]["messages"][0]["content"]
    assert matching_content[0] == continuation.blocks[0]
    assert all(block.get("type") != "thinking" for block in mismatched_content)
    assert transport.requests[0]["payload"]["stream"] is True


@pytest.mark.asyncio
async def test_anthropic_provider_rejects_unsupported_image_without_request() -> None:
    transport = FakeTransport({})
    provider = AnthropicCompatibleProvider(
        base_url="https://api.example.test",
        transport=transport,
    )
    message = MessageRecord(
        session_id="ses_1",
        role="user",
        content=[ImageContent(uri="https://example.test/image.png")],
    )

    events = [
        event
        async for event in provider.stream(
            model=ModelMetadata(
                model_id="claude-test",
                provider_id="anthropic",
                supports_vision=False,
            ),
            messages=[message],
        )
    ]

    assert [event.type for event in events] == ["error"]
    assert events[0].error == "model endpoint does not support image input"
    assert transport.requests == []
