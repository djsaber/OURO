from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from ouro.core.messages import ImageContent, ModelMessage, TextContent
from ouro.core.provider import ToolCallDelta, Usage
from ouro.core.tool_result_projection import project_provider_messages
from ouro.providers.tool_content import ToolContentPolicy


def messages_to_anthropic_messages(
    messages: list[ModelMessage],
    *,
    system_prompt: str,
    supports_inline_system_messages: bool = False,
    supports_image_input: bool,
    provider_id: str,
    model_id: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Project canonical history into Anthropic's top-level system/messages shape."""

    converted: list[dict[str, Any]] = []
    previous_was_result = False
    policy = ToolContentPolicy(
        native=frozenset({"text", "image"} if supports_image_input else {"text"})
    )
    instructions, dialogue = project_provider_messages(
        messages,
        policy,
        system_prompt=system_prompt,
        supports_inline_system_messages=supports_inline_system_messages,
    )
    for message in dialogue:
        if any(not isinstance(part, TextContent | ImageContent) for part in message.content):
            raise ValueError("unsupported Anthropic Messages content type")
        item = _message_to_anthropic(
            message,
            supports_image_input=supports_image_input,
            provider_id=provider_id,
            model_id=model_id,
        )
        if item["role"] == "assistant" and not item["content"]:
            continue
        if previous_was_result and message.role == "tool_result":
            converted[-1]["content"].extend(item["content"])
        else:
            converted.append(item)
        previous_was_result = message.role == "tool_result"
    return instructions, converted


def tool_to_anthropic_messages(tool: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": tool["name"],
        "description": tool.get("description") or "",
        "input_schema": tool.get("parameters_schema") or {"type": "object", "properties": {}},
    }


def tool_use_delta(block: Mapping[str, Any], index: int, *, streaming: bool) -> ToolCallDelta:
    call_id = block["id"] if isinstance(block.get("id"), str) else ""
    name = block["name"] if isinstance(block.get("name"), str) else ""
    value = block.get("input")
    arguments = None
    # A streamed block can open with an empty object before its JSON deltas.
    if "input" in block and not (streaming and value == {}):
        arguments = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return ToolCallDelta(
        index=index,
        id_delta=call_id,
        name_delta=name,
        arguments_delta=arguments,
    )


def content_blocks_from_response(
    response: Mapping[str, Any],
) -> tuple[list[str], list[str], list[ToolCallDelta]]:
    raw_content = response.get("content")
    if not isinstance(raw_content, list):
        return [], [], []

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[ToolCallDelta] = []
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
            tool_calls.append(tool_use_delta(block, len(tool_calls), streaming=False))
    return text_parts, reasoning_parts, tool_calls


def continuation_blocks_from_response(
    response: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    raw_content = response.get("content")
    if not isinstance(raw_content, list):
        return ()
    return ({"content": deepcopy(raw_content)},) if raw_content else ()


_USAGE_COUNTS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def usage_from_anthropic(raw: object) -> Usage | None:
    if not isinstance(raw, Mapping) or all(raw.get(name) is None for name in _USAGE_COUNTS):
        return None
    uncached_input_tokens = int(raw.get("input_tokens") or 0)
    cache_read_tokens = int(raw.get("cache_read_input_tokens") or 0)
    cache_write_tokens = int(raw.get("cache_creation_input_tokens") or 0)
    return Usage(
        input_tokens=uncached_input_tokens + cache_read_tokens + cache_write_tokens,
        output_tokens=int(raw.get("output_tokens") or 0),
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )


def merge_usage(current: dict[str, Any], report: object) -> None:
    if not isinstance(report, Mapping):
        return
    for name in _USAGE_COUNTS:
        count = report.get(name)
        if count is not None:
            current[name] = count


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

    role = message.role
    if role == "assistant":
        continuation = message.provider_continuation
        if continuation is not None and continuation.matches(
            provider_id=provider_id,
            protocol="anthropic-messages",
            model_id=model_id,
        ):
            if len(continuation.blocks) != 1 or not isinstance(
                continuation.blocks[0].get("content"), list
            ):
                raise ValueError("Anthropic continuation requires complete response content")
            content = []
            calls = []
            for block in continuation.blocks[0]["content"]:
                if isinstance(block, Mapping) and block.get("type") == "tool_use":
                    # Older sessions committed only identified calls, with {} for non-objects.
                    call_id, name = block.get("id"), block.get("name")
                    if not isinstance(call_id, str) or not call_id:
                        continue
                    if not isinstance(name, str) or not name:
                        continue
                    if not isinstance(block.get("input"), dict):
                        block = {**block, "input": {}}
                    calls.append((call_id, name, block["input"]))
                content.append(block)
            if calls != [(call.id, call.name, call.arguments) for call in message.tool_calls]:
                raise ValueError("Anthropic continuation does not match tool calls")
            return {"role": role, "content": deepcopy(content)}
    content = _message_content(message, supports_image_input=supports_image_input)
    if role == "assistant":
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
