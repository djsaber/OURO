from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ouro.core.messages import ImageContent, MessageRecord, TextContent
from ouro.core.provider import Usage


def _message_to_openai(
    message: MessageRecord,
    *,
    supports_vision: bool = False,
) -> dict[str, Any]:
    role = message.role
    if role == "system_synthetic":
        openai_role = "system"
    elif role == "tool_result":
        openai_role = "tool"
    else:
        openai_role = role

    converted: dict[str, Any] = {
        "role": openai_role,
        "content": _message_content_to_openai(message, supports_vision=supports_vision),
    }
    if openai_role == "assistant":
        tool_calls = message.metadata.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            converted["tool_calls"] = [_tool_call_message_to_openai(item) for item in tool_calls]
        if "reasoning_content" in message.metadata:
            converted["reasoning_content"] = str(message.metadata["reasoning_content"])
    if openai_role == "tool":
        converted["tool_call_id"] = str(message.metadata.get("tool_call_id") or "unknown")
    return converted


def _message_content_to_openai(
    message: MessageRecord,
    *,
    supports_vision: bool,
) -> str | list[dict[str, Any]]:
    has_image = any(isinstance(block, ImageContent) for block in message.content)
    if has_image and not supports_vision:
        raise ValueError("model endpoint does not support image input")
    if not has_image:
        return message.text_content

    content: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextContent):
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageContent):
            content.append({"type": "image_url", "image_url": {"url": block.uri}})
    return content


def _tool_to_openai(tool: Mapping[str, Any]) -> dict[str, Any]:
    parameters = tool.get("parameters_schema") or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description") or "",
            "parameters": parameters,
        },
    }


def _tool_call_message_to_openai(tool_call: object) -> dict[str, Any]:
    if not isinstance(tool_call, Mapping):
        return {
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": "{}"},
        }
    arguments = tool_call.get("arguments") or {}
    return {
        "id": str(tool_call.get("id") or ""),
        "type": "function",
        "function": {
            "name": str(tool_call.get("name") or ""),
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def _first_choice(response: Mapping[str, Any]) -> Mapping[str, Any] | None:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    return choice if isinstance(choice, Mapping) else None


def _error_from_response(response: Mapping[str, Any]) -> str:
    error = response.get("error")
    if isinstance(error, str):
        return error
    if not isinstance(error, Mapping):
        return ""
    message = error.get("message")
    code = error.get("code")
    if isinstance(message, str) and message:
        if isinstance(code, str | int | float) and code:
            return f"{message} ({code})"
        return message
    return json.dumps(dict(error), ensure_ascii=False)


def _content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _reasoning_content_to_text(reasoning_content: object) -> str:
    """Extract reasoning text from a message/delta reasoning_content field.

    Providers such as DeepSeek return reasoning_content as either a plain string
    or an array of content blocks. This helper mirrors _content_to_text() so
    reasoning deltas are handled consistently with regular content.
    """
    if isinstance(reasoning_content, str):
        return reasoning_content
    if not isinstance(reasoning_content, list):
        return ""

    parts: list[str] = []
    for block in reasoning_content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _tool_calls_from_message(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_tool_calls = message.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return []

    tool_calls: list[dict[str, Any]] = []
    for raw_tool_call in raw_tool_calls:
        if not isinstance(raw_tool_call, Mapping):
            continue
        function = raw_tool_call.get("function") or {}
        if not isinstance(function, Mapping):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        tool_calls.append(
            {
                "id": str(raw_tool_call.get("id") or ""),
                "name": name,
                "arguments": _arguments_to_dict(function.get("arguments")),
            }
        )
    return tool_calls


def _tool_call_delta_parts(delta: Mapping[str, Any]) -> list[tuple[int, dict[str, str]]]:
    raw_tool_calls = delta.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return []

    parts: list[tuple[int, dict[str, str]]] = []
    for fallback_index, raw_tool_call in enumerate(raw_tool_calls):
        if not isinstance(raw_tool_call, Mapping):
            continue
        raw_index = raw_tool_call.get("index")
        index = raw_index if isinstance(raw_index, int) else fallback_index
        function = raw_tool_call.get("function") or {}
        if not isinstance(function, Mapping):
            function = {}
        part = {
            "id": str(raw_tool_call.get("id") or ""),
            "name": str(function.get("name") or ""),
            "arguments": str(function.get("arguments") or ""),
        }
        parts.append((index, part))
    return parts


def _tool_calls_from_stream_parts(
    tool_call_parts: Mapping[int, Mapping[str, str]],
) -> list[dict[str, Any]]:
    tool_calls: list[dict[str, Any]] = []
    for index in sorted(tool_call_parts):
        part = tool_call_parts[index]
        name = part.get("name") or ""
        if not name:
            continue
        tool_calls.append(
            {
                "id": part.get("id") or "",
                "name": name,
                "arguments": _arguments_to_dict(part.get("arguments")),
            }
        )
    return tool_calls


def _arguments_to_dict(arguments: object) -> dict[str, Any]:
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if isinstance(arguments, str) and arguments.strip():
        parsed = json.loads(arguments)
        if isinstance(parsed, dict):
            return parsed
    return {}


def _usage_from_response_or_none(response: Mapping[str, Any]) -> Usage | None:
    if not isinstance(response.get("usage"), Mapping):
        return None
    return _usage_from_response(response)


def _usage_from_response(response: Mapping[str, Any]) -> Usage:
    raw_usage = response.get("usage")
    if not isinstance(raw_usage, Mapping):
        return Usage()
    prompt_details = raw_usage.get("prompt_tokens_details") or {}
    cached_tokens = 0
    if isinstance(prompt_details, Mapping):
        cached_tokens = int(prompt_details.get("cached_tokens") or 0)
    return Usage(
        input_tokens=int(raw_usage.get("prompt_tokens") or raw_usage.get("input_tokens") or 0),
        output_tokens=int(
            raw_usage.get("completion_tokens") or raw_usage.get("output_tokens") or 0
        ),
        cache_read_tokens=cached_tokens,
    )
