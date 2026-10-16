from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from ouro.core.content import (
    AudioContent,
    MediaContent,
    MediaModality,
    VideoContent,
    input_modalities,
    media_format_omission,
)
from ouro.core.messages import ImageContent, ModelMessage, TextContent
from ouro.core.provider import ToolCallDelta, Usage
from ouro.core.tool_result_projection import project_tool_result_messages
from ouro.providers.tool_content import ToolContentPolicy

ChatCompletionsMessageContent = str | list[dict[str, Any]]


def messages_to_chat_completions(
    messages: list[ModelMessage],
    *,
    system_prompt: str,
    supports_image_input: bool,
    supports_audio_input: bool = False,
    supports_video_input: bool = False,
    provider_id: str,
    model_id: str,
    include_continuation: bool,
) -> list[dict[str, Any]]:
    """Encode one request with at most one leading Chat Completions system message."""

    system_contents: list[ChatCompletionsMessageContent] = [system_prompt] if system_prompt else []
    converted_messages: list[dict[str, Any]] = []
    modalities = input_modalities(
        supports_image_input=supports_image_input,
        supports_audio_input=supports_audio_input,
        supports_video_input=supports_video_input,
    )
    policy = ToolContentPolicy(
        native=frozenset({"text"}),
        contextual=modalities,
    )
    for message in project_tool_result_messages(messages, policy):
        converted = _message_to_chat_completions(
            message,
            modalities=modalities,
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


def _merge_system_contents(
    contents: list[ChatCompletionsMessageContent],
) -> ChatCompletionsMessageContent:
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


def _message_to_chat_completions(
    message: ModelMessage,
    *,
    modalities: frozenset[MediaModality],
    provider_id: str,
    model_id: str,
    include_continuation: bool,
) -> dict[str, Any]:
    role = message.role
    if role == "tool_result":
        wire_role = "tool"
    else:
        wire_role = role

    converted: dict[str, Any] = {
        "role": wire_role,
        "content": _message_content_to_chat_completions(message, modalities=modalities),
    }
    if wire_role == "assistant":
        if message.tool_calls:
            converted["tool_calls"] = [
                _tool_call_message_to_chat_completions(item.model_dump(mode="json"))
                for item in message.tool_calls
            ]
        continuation = message.provider_continuation
        if (
            include_continuation
            and continuation is not None
            and continuation.matches(
                provider_id=provider_id,
                protocol="openai-chat-completions",
                model_id=model_id,
            )
        ):
            for block in continuation.blocks:
                converted.update(reasoning_fields_from_response(block))
    if wire_role == "tool":
        if message.tool_result is None:
            raise ValueError("tool_result message is missing its tool result reference")
        converted["tool_call_id"] = message.tool_result.tool_call_id
    return converted


def _message_content_to_chat_completions(
    message: ModelMessage,
    *,
    modalities: frozenset[MediaModality],
) -> str | list[dict[str, Any]]:
    if not any(isinstance(block, MediaContent) for block in message.content):
        if any(not isinstance(block, TextContent) for block in message.content):
            raise ValueError("unsupported Chat Completions content type")
        return message.text_content

    content: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextContent):
            content.append({"type": "text", "text": block.text})
            continue
        if not isinstance(block, MediaContent):
            raise ValueError("unsupported Chat Completions content type")
        if block.type not in modalities:
            raise ValueError(f"model endpoint does not support {block.type} input")
        if message.role != "user":
            raise ValueError(f"{block.type} input requires a user message")
        if isinstance(block, ImageContent):
            content.append({"type": "image_url", "image_url": {"url": block.uri}})
        elif isinstance(block, AudioContent):
            reason = media_format_omission(block.type, block.media_type)
            if reason:
                raise ValueError(reason)
            prefix = f"data:{block.media_type};base64,"
            if not block.uri.startswith(prefix) or len(block.uri) == len(prefix):
                raise ValueError("audio input requires a base64 data URI matching its media type")
            content.append(
                {
                    "type": "input_audio",
                    "input_audio": {
                        "data": block.uri[len(prefix) :],
                        "format": {"audio/wav": "wav", "audio/mpeg": "mp3"}[block.media_type],
                    },
                }
            )
        elif isinstance(block, VideoContent):
            reason = media_format_omission(block.type, block.media_type)
            if reason:
                raise ValueError(reason)
            prefix = f"data:{block.media_type};base64,"
            if block.uri == prefix or not block.uri.startswith((prefix, "https://", "http://")):
                raise ValueError("video input requires an HTTP URL or matching base64 data URI")
            content.append({"type": "video_url", "video_url": {"url": block.uri}})
    return content


def _tool_to_chat_completions(tool: Mapping[str, Any]) -> dict[str, Any]:
    parameters = tool.get("parameters_schema") or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description") or "",
            "parameters": parameters,
        },
    }


def _tool_call_message_to_chat_completions(tool_call: object) -> dict[str, Any]:
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


def reasoning_fields_from_response(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep supported extension fields in their native shape, separately from display text."""
    return {
        field: deepcopy(value)
        for field in _REASONING_FIELDS
        if isinstance(value := payload.get(field), str | list) and value
    }


def _reasoning_field_value(payload: Mapping[str, Any]) -> object | None:
    """Pick the reasoning payload from an Chat Completions message or delta.

    Chat Completions services split across two field conventions: DeepSeek-style
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
