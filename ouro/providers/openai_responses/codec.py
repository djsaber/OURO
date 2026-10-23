"""Portable canonical history with compatible, complete Responses replay items."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from ouro.core.messages import (
    ImageContent,
    ModelMessage,
    ProviderContinuationArtifact,
    TextContent,
)
from ouro.core.provider import Usage
from ouro.core.tool_result_projection import project_tool_result_messages
from ouro.providers.openai_responses.errors import OpenAIResponsesError
from ouro.providers.openai_responses.items import OpenAIResponsesItemAccumulator
from ouro.providers.tool_content import ToolContentPolicy

PROTOCOL = "openai-responses"


def tool_arguments(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        raise OpenAIResponsesError("invalid_tool_arguments")
    try:
        # Reject non-JSON numeric constants rather than executing altered arguments.
        parsed = json.loads(
            value, parse_constant=lambda _: _invalid_arguments(), object_pairs_hook=_unique_object
        )
        json.dumps(parsed, allow_nan=False)
    except ValueError:
        raise OpenAIResponsesError("invalid_tool_arguments") from None
    if not isinstance(parsed, dict):
        raise OpenAIResponsesError("invalid_tool_arguments")
    return parsed


def _invalid_arguments() -> None:
    raise ValueError


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def output_item(item: object) -> dict[str, Any]:
    """Validate supported semantics, retaining every original field for faithful replay."""
    if not isinstance(item, dict) or item.get("status") not in (None, "completed"):
        raise OpenAIResponsesError("invalid_output_item")
    kind = item.get("type")
    if not isinstance(kind, str):
        raise OpenAIResponsesError("invalid_output_item")
    if kind == "function_call":
        if any(not isinstance(item.get(key), str) or not item[key] for key in ("call_id", "name")):
            raise OpenAIResponsesError("invalid_output_item")
        tool_arguments(item.get("arguments"))
    elif kind in {"message", "reasoning"}:
        if kind == "message" and item.get("role") != "assistant":
            raise OpenAIResponsesError("invalid_output_item")
        fields = (
            (("content", {"output_text", "refusal"}),)
            if kind == "message"
            else (("summary", {"summary_text"}), ("content", {"reasoning_text"}))
        )
        for field, allowed in fields:
            parts = item.get(field)
            if kind == "reasoning" and parts is None:
                continue
            if not isinstance(parts, list):
                raise OpenAIResponsesError("invalid_output_item")
            for part in parts:
                if not isinstance(part, dict):
                    raise OpenAIResponsesError("invalid_output_item")
                if not isinstance(part.get("type"), str) or part["type"] not in allowed:
                    raise OpenAIResponsesError("unsupported_output_item")
                key = "refusal" if part["type"] == "refusal" else "text"
                if not isinstance(part.get(key), str):
                    raise OpenAIResponsesError("invalid_output_item")
        if kind == "reasoning" and item.get("encrypted_content") is not None:
            if not isinstance(item["encrypted_content"], str):
                raise OpenAIResponsesError("invalid_output_item")
    else:
        raise OpenAIResponsesError("unsupported_output_item")
    # Function calls are correlated through call_id; their separate item id is
    # optional in Responses. Message/reasoning ids remain required for replay.
    item_id = item.get("id")
    if item_id is None and kind == "function_call":
        return item
    if not isinstance(item_id, str) or not item_id:
        raise OpenAIResponsesError("invalid_output_item")
    return item


def continuation_from_output(
    items: list[dict[str, Any]], *, provider_id: str, model_id: str, scope: tuple[str, ...]
) -> ProviderContinuationArtifact:
    if not items or not scope or any(not item for item in scope):
        raise OpenAIResponsesError("invalid_continuation")
    for item in items:
        output_item(item)
    return ProviderContinuationArtifact(
        provider_id=provider_id,
        protocol=PROTOCOL,
        model_id=model_id,
        blocks=({"format_version": 1, "scope": list(scope), "items": deepcopy(items)},),
    )


def _replay_if_compatible(
    artifact: ProviderContinuationArtifact,
    *,
    provider_id: str,
    model_id: str,
    scope: tuple[str, ...],
) -> list[dict[str, Any]] | None:
    """Select native replay without making canonical history depend on its origin.

    Foreign artifacts belong to their source codec. A matching route still
    requires a readable envelope and valid items before any native replay.
    """
    if not artifact.matches(provider_id=provider_id, protocol=PROTOCOL, model_id=model_id):
        return None
    if len(artifact.blocks) != 1:
        raise OpenAIResponsesError("invalid_continuation")
    envelope = artifact.blocks[0]
    if (
        set(envelope) != {"format_version", "scope", "items"}
        or type(envelope.get("format_version")) is not int
        or envelope["format_version"] != 1
        or not isinstance(envelope.get("scope"), list)
        or not envelope["scope"]
        or any(not isinstance(part, str) or not part for part in envelope["scope"])
        or not isinstance(envelope.get("items"), list)
        or not envelope["items"]
    ):
        raise OpenAIResponsesError("invalid_continuation")
    if envelope["scope"] != list(scope):
        return None
    items = OpenAIResponsesItemAccumulator().finish(envelope["items"])
    return [output_item(item) for item in items]


def responses_input(
    messages: list[ModelMessage],
    *,
    system_prompt: str,
    provider_id: str,
    model_id: str,
    scope: tuple[str, ...],
    supports_image_input: bool = False,
) -> dict[str, Any]:
    """Encode only content fields. Service capabilities and model options belong to the adapter."""
    instructions = [system_prompt] if system_prompt else []
    items: list[dict[str, Any]] = []
    policy = ToolContentPolicy(
        native=frozenset({"text", "image"} if supports_image_input else {"text"})
    )
    try:
        projected = project_tool_result_messages(messages, policy)
    except ValueError:
        raise OpenAIResponsesError("unsupported_input") from None
    for message in projected:
        has_image = any(isinstance(part, ImageContent) for part in message.content)
        if any(not isinstance(part, TextContent | ImageContent) for part in message.content):
            raise OpenAIResponsesError("unsupported_input")
        if has_image and (not supports_image_input or message.role not in {"user", "tool_result"}):
            raise OpenAIResponsesError("unsupported_input")
        if message.role == "system":
            instructions.append(message.text_content)
            continue
        if message.role == "tool_result":
            if message.tool_result is None or not message.tool_result.tool_call_id:
                raise OpenAIResponsesError("unsupported_input")
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_result.tool_call_id,
                    "output": _input_content(message) if has_image else message.text_content,
                }
            )
            continue
        replay = (
            _replay_if_compatible(
                message.provider_continuation,
                provider_id=provider_id,
                model_id=model_id,
                scope=scope,
            )
            if message.provider_continuation is not None
            else None
        )
        if replay is not None:
            calls = [
                (item["call_id"], item["name"], tool_arguments(item["arguments"]))
                for item in replay
                if item["type"] == "function_call"
            ]
            if calls != [(call.id, call.name, call.arguments) for call in message.tool_calls]:
                raise OpenAIResponsesError("continuation_tool_mismatch")
            if len({call[0] for call in calls}) != len(calls):
                raise OpenAIResponsesError("duplicate_tool_call")
            items.extend(replay)
        else:
            if message.content or not message.tool_calls:
                content: str | list[dict[str, Any]] = message.text_content
                if has_image:
                    content = _input_content(message)
                items.append({"role": message.role, "content": content})
            for call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments, ensure_ascii=False, allow_nan=False
                        ),
                    }
                )
    return {"instructions": "\n\n".join(instructions), "input": items}


def _input_content(message: ModelMessage) -> list[dict[str, Any]]:
    return [
        {"type": "input_text", "text": part.text}
        if isinstance(part, TextContent)
        else {"type": "input_image", "image_url": part.uri, "detail": "auto"}
        for part in message.content
        if isinstance(part, TextContent | ImageContent)
    ]


def responses_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for tool in tools:
        name, parameters = tool.get("name"), tool.get("parameters_schema")
        if not isinstance(name, str) or not name or not isinstance(parameters, dict):
            raise OpenAIResponsesError("invalid_tool_schema")
        # OURO schemas need not satisfy OpenAI strict-schema restrictions. Preserve
        # optional arguments and additionalProperties instead of rewriting tool contracts.
        result.append(
            {
                "type": "function",
                "name": name,
                "description": tool.get("description") or "",
                "parameters": deepcopy(parameters),
                "strict": False,
            }
        )
    return result


def responses_usage(raw: object) -> Usage | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OpenAIResponsesError("invalid_usage")
    input_tokens, output_tokens = raw.get("input_tokens"), raw.get("output_tokens")
    if (
        type(input_tokens) is not int
        or input_tokens < 0
        or type(output_tokens) is not int
        or output_tokens < 0
    ):
        raise OpenAIResponsesError("invalid_usage")
    details = raw.get("input_tokens_details")
    if details is None:
        details = {}
    if not isinstance(details, dict):
        raise OpenAIResponsesError("invalid_usage")
    cached = details.get("cached_tokens", 0)
    if type(cached) is not int or not 0 <= cached <= input_tokens:
        raise OpenAIResponsesError("invalid_usage")
    written = details.get("cache_write_tokens", 0)
    if type(written) is not int or not 0 <= written <= input_tokens:
        raise OpenAIResponsesError("invalid_usage")
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cached,
        cache_write_tokens=written,
    )
