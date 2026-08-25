from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ouro.core.messages import ImageContent, ModelMessage, TextContent, model_visible_message
from ouro.core.provider import ToolCallDelta, Usage
from ouro.core.tools import ToolCallRequest

OpenAIMessageContent = str | list[dict[str, Any]]


def messages_to_openai(
    messages: list[ModelMessage],
    *,
    system_prompt: str,
    supports_vision: bool,
    provider_id: str,
    model_id: str,
    include_continuation: bool,
) -> list[dict[str, Any]]:
    """Encode one request with at most one leading OpenAI system message."""

    system_contents: list[OpenAIMessageContent] = [system_prompt] if system_prompt else []
    converted_messages: list[dict[str, Any]] = []
    for message in messages:
        converted = _message_to_openai(
            message,
            supports_vision=supports_vision,
            provider_id=provider_id,
            model_id=model_id,
            include_continuation=include_continuation,
        )
        if converted["role"] == "system":
            system_contents.append(converted["content"])
        else:
            converted_messages.append(converted)
    if system_contents:
        converted_messages.insert(
            0,
            {"role": "system", "content": _merge_system_contents(system_contents)},
        )
    return converted_messages


def _merge_system_contents(contents: list[OpenAIMessageContent]) -> OpenAIMessageContent:
    if all(isinstance(content, str) for content in contents):
        return "\n\n".join(content for content in contents if isinstance(content, str))

    merged: list[dict[str, Any]] = []
    for index, content in enumerate(contents):
        if index:
            merged.append({"type": "text", "text": "\n\n"})
        if isinstance(content, str):
            merged.append({"type": "text", "text": content})
        else:
            merged.extend(content)
    return merged


def _message_to_openai(
    message: ModelMessage,
    *,
    supports_vision: bool = False,
    provider_id: str,
    model_id: str,
    include_continuation: bool,
) -> dict[str, Any]:
    message = model_visible_message(message)
    role = message.role
    if role == "tool_result":
        openai_role = "tool"
    else:
        openai_role = role

    converted: dict[str, Any] = {
        "role": openai_role,
        "content": _message_content_to_openai(message, supports_vision=supports_vision),
    }
    if openai_role == "assistant":
        if message.tool_calls:
            converted["tool_calls"] = [
                _tool_call_message_to_openai(item.model_dump(mode="json"))
                for item in message.tool_calls
            ]
        continuation = message.provider_continuation
        if (
            include_continuation
            and message.tool_calls
            and continuation is not None
            and continuation.matches(
                provider_id=provider_id,
                protocol="openai-chat-completions",
                model_id=model_id,
            )
        ):
            for block in continuation.blocks:
                if "reasoning_content" in block:
                    converted["reasoning_content"] = block["reasoning_content"]
    if openai_role == "tool":
        if message.tool_result is None:
            raise ValueError("tool_result message is missing its tool result reference")
        converted["tool_call_id"] = message.tool_result.tool_call_id
    return converted


def _message_content_to_openai(
    message: ModelMessage,
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


_REASONING_FIELDS = ("reasoning_content", "reasoning")


def _reasoning_field_value(payload: Mapping[str, Any]) -> object | None:
    """Pick the reasoning payload from an OpenAI-protocol message or delta.

    OpenAI-compatible vendors split across two field conventions: DeepSeek-style
    `reasoning_content` and the bare `reasoning` used by OpenRouter and others.
    Both accept the string-or-blocks shapes of _reasoning_content_to_text(); the
    first field that yields non-empty text wins.
    """
    for field in _REASONING_FIELDS:
        value = payload.get(field)
        if _reasoning_content_to_text(value):
            return value
    return None


def _reasoning_content_to_text(reasoning_content: object) -> str:
    """Extract reasoning text from a message/delta reasoning field value.

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


def _tool_calls_from_message(message: Mapping[str, Any]) -> list[ToolCallRequest]:
    raw_tool_calls = message.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return []

    tool_calls: list[ToolCallRequest] = []
    for source_index, raw_tool_call in enumerate(raw_tool_calls):
        if not isinstance(raw_tool_call, Mapping):
            continue
        function = raw_tool_call.get("function") or {}
        if not isinstance(function, Mapping):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        tool_calls.append(
            ToolCallRequest(
                id=str(raw_tool_call.get("id") or ""),
                name=name,
                arguments=_arguments_to_dict(function.get("arguments")),
                source_index=source_index,
            )
        )
    return tool_calls


def _tool_call_deltas(delta: Mapping[str, Any]) -> list[ToolCallDelta]:
    raw_tool_calls = delta.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return []

    deltas: list[ToolCallDelta] = []
    for fallback_index, raw_tool_call in enumerate(raw_tool_calls):
        if not isinstance(raw_tool_call, Mapping):
            continue
        raw_index = raw_tool_call.get("index")
        index = raw_index if isinstance(raw_index, int) else fallback_index
        function = raw_tool_call.get("function") or {}
        if not isinstance(function, Mapping):
            function = {}
        id_delta = str(raw_tool_call.get("id") or "")
        name_delta = str(function.get("name") or "")
        raw_arguments = function.get("arguments")
        if isinstance(raw_arguments, str):
            arguments_delta = raw_arguments
        elif isinstance(raw_arguments, Mapping):
            arguments_delta = json.dumps(raw_arguments, separators=(",", ":"))
        else:
            arguments_delta = ""
        if not (id_delta or name_delta or arguments_delta):
            continue
        deltas.append(
            ToolCallDelta(
                index=index,
                id_delta=id_delta,
                name_delta=name_delta,
                arguments_delta=arguments_delta,
            )
        )
    return deltas


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
