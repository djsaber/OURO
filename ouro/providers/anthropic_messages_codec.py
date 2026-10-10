from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from ouro.core.messages import ImageContent, ModelMessage, TextContent
from ouro.core.provider import Usage
from ouro.core.tool_result_projection import project_tool_result_messages
from ouro.core.tools import ToolCallRequest
from ouro.providers.tool_content import ToolContentPolicy


def messages_to_anthropic_messages(
    messages: list[ModelMessage],
    *,
    system_prompt: str,
    supports_image_input: bool,
    provider_id: str,
    model_id: str,
    include_continuation: bool,
) -> tuple[str, list[dict[str, Any]]]:
    """Project canonical history into Anthropic's top-level system/messages shape."""

    system_parts = [system_prompt] if system_prompt else []
    converted: list[dict[str, Any]] = []
    policy = ToolContentPolicy(
        native=frozenset({"text", "image"} if supports_image_input else {"text"})
    )
    for message in project_tool_result_messages(messages, policy):
        if any(not isinstance(part, TextContent | ImageContent) for part in message.content):
            raise ValueError("unsupported Anthropic Messages content type")
        if message.role == "system":
            if message.text_content:
                system_parts.append(message.text_content)
            continue
        item = _message_to_anthropic(
            message,
            supports_image_input=supports_image_input,
            provider_id=provider_id,
            model_id=model_id,
            include_continuation=include_continuation,
        )
        if converted and converted[-1]["role"] == item["role"]:
            converted[-1]["content"].extend(item["content"])
        else:
            converted.append(item)
    return "\n\n".join(system_parts), converted


def tool_to_anthropic_messages(tool: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": tool["name"],
        "description": tool.get("description") or "",
        "input_schema": tool.get("parameters_schema") or {"type": "object", "properties": {}},
    }


def content_blocks_from_response(
    response: Mapping[str, Any],
) -> tuple[list[str], list[str], list[ToolCallRequest]]:
    raw_content = response.get("content")
    if not isinstance(raw_content, list):
        return [], [], []

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[ToolCallRequest] = []
    for block in raw_content:
        if not isinstance(block, Mapping):
            continue
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            text_parts.append(str(block["text"]))
        elif block_type in {"thinking", "redacted_thinking"}:
            thinking = block.get("thinking")
            if isinstance(thinking, str) and thinking:
                reasoning_parts.append(thinking)
        elif block_type == "tool_use":
            name = block.get("name")
            call_id = block.get("id")
            if not isinstance(name, str) or not name or not isinstance(call_id, str) or not call_id:
                continue
            arguments = block.get("input")
            tool_calls.append(
                ToolCallRequest(
                    id=call_id,
                    name=name,
                    arguments=dict(arguments) if isinstance(arguments, Mapping) else {},
                    source_index=len(tool_calls),
                )
            )
    return text_parts, reasoning_parts, tool_calls


def continuation_blocks_from_response(
    response: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    raw_content = response.get("content")
    if not isinstance(raw_content, list):
        return ()
    return tuple(
        deepcopy(dict(block))
        for block in raw_content
        if isinstance(block, Mapping) and block.get("type") in {"thinking", "redacted_thinking"}
    )


def usage_from_anthropic(value: object) -> Usage:
    raw = value if isinstance(value, Mapping) else {}
    uncached_input_tokens = int(raw.get("input_tokens") or 0)
    cache_read_tokens = int(raw.get("cache_read_input_tokens") or 0)
    cache_write_tokens = int(raw.get("cache_creation_input_tokens") or 0)
    return Usage(
        input_tokens=uncached_input_tokens + cache_read_tokens + cache_write_tokens,
        output_tokens=int(raw.get("output_tokens") or 0),
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )


def merge_usage(current: Usage, value: object) -> Usage:
    incoming = usage_from_anthropic(value)
    return Usage(
        input_tokens=incoming.input_tokens or current.input_tokens,
        output_tokens=incoming.output_tokens or current.output_tokens,
        cache_read_tokens=incoming.cache_read_tokens or current.cache_read_tokens,
        cache_write_tokens=incoming.cache_write_tokens or current.cache_write_tokens,
    )


def error_from_anthropic(response: Mapping[str, Any]) -> str:
    error = response.get("error")
    if isinstance(error, str):
        return error
    if not isinstance(error, Mapping):
        return ""
    message = error.get("message")
    error_type = error.get("type")
    if isinstance(message, str) and message:
        return f"{message} ({error_type})" if isinstance(error_type, str) else message
    return json.dumps(dict(error), ensure_ascii=False)


def _message_to_anthropic(
    message: ModelMessage,
    *,
    supports_image_input: bool,
    provider_id: str,
    model_id: str,
    include_continuation: bool,
) -> dict[str, Any]:
    if message.role == "tool_result":
        if message.tool_result is None:
            raise ValueError("tool_result message is missing its tool result reference")
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_result.tool_call_id,
                    "content": (
                        _message_content(message, supports_image_input=supports_image_input)
                        if any(isinstance(part, ImageContent) for part in message.content)
                        else message.text_content
                    ),
                    "is_error": message.tool_result.is_error,
                }
            ],
        }

    role = "assistant" if message.role == "assistant" else "user"
    content = _message_content(message, supports_image_input=supports_image_input)
    if role == "assistant":
        continuation = message.provider_continuation
        if (
            include_continuation
            and message.tool_calls
            and continuation is not None
            and continuation.matches(
                provider_id=provider_id,
                protocol="anthropic-messages",
                model_id=model_id,
            )
        ):
            content = [*(deepcopy(block) for block in continuation.blocks), *content]
        content.extend(
            {
                "type": "tool_use",
                "id": tool_call.id,
                "name": tool_call.name,
                "input": tool_call.arguments,
            }
            for tool_call in message.tool_calls
        )
    return {"role": role, "content": content}


def _message_content(
    message: ModelMessage,
    *,
    supports_image_input: bool,
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextContent):
            if block.text:
                content.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageContent):
            if not supports_image_input:
                raise ValueError("model endpoint does not support image input")
            content.append(_image_to_anthropic(block))
        else:
            raise ValueError(f"Anthropic Messages does not support {block.type} input")
    return content


def _image_to_anthropic(image: ImageContent) -> dict[str, Any]:
    if image.uri.startswith("data:"):
        header, separator, data = image.uri.partition(",")
        if not separator or ";base64" not in header:
            raise ValueError("Anthropic image data URI must use base64 encoding")
        media_type = image.media_type or header.removeprefix("data:").split(";", 1)[0]
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    if image.uri.startswith(("https://", "http://")):
        return {"type": "image", "source": {"type": "url", "url": image.uri}}
    raise ValueError("Anthropic image input must be an HTTP URL or base64 data URI")
