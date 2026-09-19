"""Canonical text/tools and route-scoped, complete Responses replay items."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from ouro.core.messages import (
    ModelMessage,
    ProviderContinuationArtifact,
    TextContent,
    model_visible_message,
)
from ouro.core.provider import Usage
from ouro.providers.responses_errors import ResponsesError
from ouro.providers.responses_items import ResponseItems

PROTOCOL = "openai-responses"


def tool_arguments(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        raise ResponsesError("invalid_tool_arguments")
    try:
        # Reject non-JSON numeric constants rather than executing altered arguments.
        parsed = json.loads(
            value, parse_constant=lambda _: _invalid_arguments(), object_pairs_hook=_unique_object
        )
        json.dumps(parsed, allow_nan=False)
    except ValueError:
        raise ResponsesError("invalid_tool_arguments") from None
    if not isinstance(parsed, dict):
        raise ResponsesError("invalid_tool_arguments")
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
        raise ResponsesError("invalid_output_item")
    kind = item.get("type")
    if not isinstance(kind, str):
        raise ResponsesError("invalid_output_item")
    if kind == "function_call":
        if any(not isinstance(item.get(key), str) or not item[key] for key in ("call_id", "name")):
            raise ResponsesError("invalid_output_item")
        tool_arguments(item.get("arguments"))
    elif kind in {"message", "reasoning"}:
        if kind == "message" and item.get("role") != "assistant":
            raise ResponsesError("invalid_output_item")
        parts = item.get("content") if kind == "message" else item.get("summary", [])
        if not isinstance(parts, list):
            raise ResponsesError("invalid_output_item")
        for part in parts:
            if not isinstance(part, dict):
                raise ResponsesError("invalid_output_item")
            allowed = {"output_text", "refusal"} if kind == "message" else {"summary_text"}
            if not isinstance(part.get("type"), str) or part["type"] not in allowed:
                raise ResponsesError("unsupported_output_item")
            key = "refusal" if part["type"] == "refusal" else "text"
            if not isinstance(part.get(key), str):
                raise ResponsesError("invalid_output_item")
        if kind == "reasoning" and item.get("encrypted_content") is not None:
            if not isinstance(item["encrypted_content"], str):
                raise ResponsesError("invalid_output_item")
    else:
        raise ResponsesError("unsupported_output_item")
    # Function calls are correlated through call_id; their separate item id is
    # optional in Responses. Message/reasoning ids remain required for replay.
    item_id = item.get("id")
    if item_id is None and kind == "function_call":
        return item
    if not isinstance(item_id, str) or not item_id:
        raise ResponsesError("invalid_output_item")
    return item


def continuation_from_output(
    items: list[dict[str, Any]], *, provider_id: str, model_id: str, scope: tuple[str, ...]
) -> ProviderContinuationArtifact:
    if not items or not scope or any(not item for item in scope):
        raise ResponsesError("invalid_continuation")
    for item in items:
        output_item(item)
    return ProviderContinuationArtifact(
        provider_id=provider_id,
        protocol=PROTOCOL,
        model_id=model_id,
        blocks=({"format_version": 1, "scope": list(scope), "items": deepcopy(items)},),
    )


def _replay(
    artifact: ProviderContinuationArtifact,
    *,
    provider_id: str,
    model_id: str,
    scope: tuple[str, ...],
) -> list[dict[str, Any]]:
    if not artifact.matches(provider_id=provider_id, protocol=PROTOCOL, model_id=model_id):
        raise ResponsesError("continuation_route_mismatch")
    if len(artifact.blocks) != 1:
        raise ResponsesError("invalid_continuation")
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
        raise ResponsesError("invalid_continuation")
    if envelope["scope"] != list(scope):
        raise ResponsesError("continuation_scope_mismatch")
    items = ResponseItems().finish(envelope["items"])
    return [output_item(item) for item in items]


def responses_input(
    messages: list[ModelMessage],
    *,
    system_prompt: str,
    provider_id: str,
    model_id: str,
    scope: tuple[str, ...],
) -> dict[str, Any]:
    """Encode only content fields. Service capabilities and model options belong to the adapter."""
    instructions = [system_prompt] if system_prompt else []
    items: list[dict[str, Any]] = []
    for original in messages:
        message = model_visible_message(original)
        if any(not isinstance(part, TextContent) for part in message.content):
            raise ResponsesError("unsupported_input")
        if message.role == "system":
            instructions.append(message.text_content)
        elif message.role == "tool_result":
            if message.tool_result is None or not message.tool_result.tool_call_id:
                raise ResponsesError("unsupported_input")
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_result.tool_call_id,
                    "output": message.text_content,
                }
            )
        elif message.provider_continuation is not None:
            replay = _replay(
                message.provider_continuation,
                provider_id=provider_id,
                model_id=model_id,
                scope=scope,
            )
            calls = [
                (item["call_id"], item["name"], tool_arguments(item["arguments"]))
                for item in replay
                if item["type"] == "function_call"
            ]
            if calls != [(call.id, call.name, call.arguments) for call in message.tool_calls]:
                raise ResponsesError("continuation_tool_mismatch")
            if len({call[0] for call in calls}) != len(calls):
                raise ResponsesError("duplicate_tool_call")
            items.extend(replay)
        else:
            if message.content or not message.tool_calls:
                items.append({"role": message.role, "content": message.text_content})
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


def responses_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for tool in tools:
        name, parameters = tool.get("name"), tool.get("parameters_schema")
        if not isinstance(name, str) or not name or not isinstance(parameters, dict):
            raise ResponsesError("invalid_tool_schema")
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
        raise ResponsesError("invalid_usage")
    input_tokens, output_tokens = raw.get("input_tokens"), raw.get("output_tokens")
    if (
        type(input_tokens) is not int
        or input_tokens < 0
        or type(output_tokens) is not int
        or output_tokens < 0
    ):
        raise ResponsesError("invalid_usage")
    details = raw.get("input_tokens_details")
    if details is None:
        details = {}
    if not isinstance(details, dict):
        raise ResponsesError("invalid_usage")
    cached = details.get("cached_tokens", 0)
    if type(cached) is not int or not 0 <= cached <= input_tokens:
        raise ResponsesError("invalid_usage")
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cached)
