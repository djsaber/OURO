"""A bounded Responses contract experiment, not a production codec or agent loop."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from dev.probes.codex_oauth import ProbeError, required_text
from ouro.auth.contracts import CredentialGrant
from ouro.providers.http_transport import HttpxProviderHTTPTransport, ProviderTransportError
from ouro.providers.openai_responses.errors import OpenAIResponsesError
from ouro.providers.openai_responses.items import OpenAIResponsesItemAccumulator

RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
_EVENT_TYPES = frozenset(
    {
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.output_item.done",
        "response.content_part.added",
        "response.content_part.done",
        "response.output_text.delta",
        "response.output_text.done",
        "response.refusal.delta",
        "response.refusal.done",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.completed",
        "response.incomplete",
        "response.failed",
        "error",
    }
)
_ITEM_TYPES = frozenset({"message", "reasoning", "function_call"})


@dataclass
class ResponseObservation:
    """Only positive allowlisted facts leave the in-memory experiment."""

    event_counts: dict[str, int]
    item_types: list[str]
    encrypted_reasoning: bool
    usage: dict[str, int]
    output: list[dict[str, Any]] = field(repr=False)

    def report(self) -> dict[str, Any]:
        content_types = []
        for item in self.output:
            if item.get("type") != "message":
                continue
            for part in item.get("content", []):
                kind = part.get("type") if isinstance(part, dict) else None
                content_types.append(kind if kind in ("output_text", "refusal") else "unknown")
        return {
            "event_counts": self.event_counts,
            "item_types": self.item_types,
            "encrypted_reasoning": self.encrypted_reasoning,
            "usage": self.usage,
            "content_types": content_types,
            "text_characters": len(_text(self.output)),
        }


def _usage(response: dict[str, Any]) -> dict[str, int]:
    raw = response.get("usage")
    if not isinstance(raw, dict):
        raise ProbeError("missing_usage")
    usage = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = raw.get(key)
        if type(value) is not int or value < 0:
            raise ProbeError("invalid_usage")
        usage[key] = value
    for parent, key in (
        ("input_tokens_details", "cached_tokens"),
        ("output_tokens_details", "reasoning_tokens"),
    ):
        details = raw.get(parent)
        if isinstance(details, dict) and key in details:
            value = details[key]
            if type(value) is not int or value < 0:
                raise ProbeError("invalid_usage")
            usage[key] = value
    return usage


async def request(
    client: httpx.AsyncClient,
    grant: CredentialGrant,
    *,
    model: str,
    input_items: list[dict[str, Any]],
    tool_round: int | None = None,
) -> ResponseObservation:
    assert grant.identity is not None and grant.identity.account_id is not None
    payload: dict[str, Any] = {
        "model": model,
        "instructions": "Follow the user's verification instructions precisely.",
        "input": input_items,
        "stream": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }
    if tool_round is not None:
        payload.update(
            {
                "tools": [
                    {
                        "type": "function",
                        "name": "ouro_probe_echo",
                        "description": "Return the verification round number.",
                        "strict": True,
                        "parameters": {
                            "type": "object",
                            "properties": {"round": {"type": "integer"}},
                            "required": ["round"],
                            "additionalProperties": False,
                        },
                    }
                ],
                "tool_choice": {"type": "function", "name": "ouro_probe_echo"},
                "parallel_tool_calls": False,
            }
        )
    headers = {
        "Authorization": "Bearer " + str(grant.secret_payload["access_token"]),
        "ChatGPT-Account-Id": grant.identity.account_id,
        "originator": "ouro-auth-probe",
        "Accept": "text/event-stream",
    }
    transport = HttpxProviderHTTPTransport(client)
    counts: dict[str, int] = {}
    terminal: dict[str, Any] | None = None
    items = OpenAIResponsesItemAccumulator()
    try:
        # Total deadline also bounds a stream that sends keepalives forever.
        async with asyncio.timeout(120):
            async for event in transport.stream_json(
                url=RESPONSES_URL, headers=headers, payload=payload, timeout=60
            ):
                kind = event.get("type")
                safe_kind = kind if isinstance(kind, str) and kind in _EVENT_TYPES else "unknown"
                counts[safe_kind] = counts.get(safe_kind, 0) + 1
                if sum(counts.values()) > 10000:
                    raise ProbeError("stream_event_limit")
                if kind in {"response.failed", "response.incomplete", "error"}:
                    raise ProbeError("response_not_completed")
                if kind in {"response.output_item.added", "response.output_item.done"}:
                    if terminal is not None:
                        raise ProbeError("output_after_terminal")
                    items.observe(
                        event.get("output_index"),
                        event.get("item"),
                        complete=kind == "response.output_item.done",
                    )
                if kind == "response.completed":
                    if terminal is not None:
                        raise ProbeError("duplicate_terminal")
                    candidate = event.get("response")
                    if not isinstance(candidate, dict) or candidate.get("status") != "completed":
                        raise ProbeError("invalid_terminal")
                    terminal = candidate
        if terminal is None:
            raise ProbeError("missing_terminal")
        output = items.finish(terminal.get("output", []))
    except OpenAIResponsesError as error:
        raise ProbeError(error.code) from None
    except ProviderTransportError as error:
        # Shared transport diagnostics may include bodies; never expose them here.
        raise ProbeError("model_http_error", status=error.status_code) from None
    except TimeoutError:
        raise ProbeError("model_timeout") from None
    return ResponseObservation(
        event_counts=counts,
        item_types=[
            item["type"] if item.get("type") in _ITEM_TYPES else "unknown" for item in output
        ],
        encrypted_reasoning=any(
            item.get("type") == "reasoning" and bool(item.get("encrypted_content"))
            for item in output
        ),
        usage=_usage(terminal),
        output=output,
    )


def _text(output: list[dict[str, Any]]) -> str:
    return "".join(
        part["text"]
        for item in output
        if item.get("type") == "message"
        for part in item.get("content", [])
        if isinstance(part, dict)
        and part.get("type") == "output_text"
        and isinstance(part.get("text"), str)
    ).strip()


async def verify_responses(
    client: httpx.AsyncClient,
    grant: CredentialGrant,
    *,
    model: str,
    observations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Keep safe partial observations even when a later assertion fails."""
    reports = observations if observations is not None else []
    plain = await request(
        client,
        grant,
        model=model,
        input_items=[
            {
                "role": "user",
                "content": "Reply with exactly OURO_OK.",
            }
        ],
    )
    reports.append(dict(scenario="minimal_sse", validated=False, **plain.report()))
    if not _contains_marker(plain.output, "OURO_OK"):
        raise ProbeError("unexpected_text_result")
    reports[-1]["validated"] = True
    history: list[dict[str, Any]] = []
    seen_calls: set[str] = set()
    for round_number in (1, 2):
        history.append(
            {
                "role": "user",
                "content": (f"Call ouro_probe_echo exactly once with round={round_number}."),
            }
        )
        observation = await request(
            client, grant, model=model, input_items=history, tool_round=round_number
        )
        reports.append(
            dict(scenario=f"tool_round_{round_number}", validated=False, **observation.report())
        )
        calls = [item for item in observation.output if item.get("type") == "function_call"]
        if len(calls) != 1:
            raise ProbeError("unexpected_tool_count")
        call = calls[0]
        call_id = required_text(call, "call_id")
        if call_id in seen_calls or call.get("name") != "ouro_probe_echo":
            raise ProbeError("unexpected_tool_identity")
        try:
            arguments = json.loads(required_text(call, "arguments"))
        except ValueError:
            raise ProbeError("invalid_tool_arguments") from None
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"round"}
            or type(arguments["round"]) is not int
            or arguments["round"] != round_number
        ):
            raise ProbeError("unexpected_tool_arguments")
        seen_calls.add(call_id)
        # Full output items, including encrypted reasoning, are replayed exactly once.
        # This local echo cannot execute arbitrary code or access user data.
        history.extend(observation.output)
        history.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps({"round": round_number, "ok": True}),
            }
        )
        reports[-1]["validated"] = True
    history.append(
        {
            "role": "user",
            "content": (
                "If both tool results report ok=true for rounds 1 and 2, "
                "reply exactly OURO_TOOLS_OK."
            ),
        }
    )
    final = await request(client, grant, model=model, input_items=history)
    reports.append(dict(scenario="tool_continuation", validated=False, **final.report()))
    if not _contains_marker(final.output, "OURO_TOOLS_OK"):
        raise ProbeError("unexpected_continuation_result")
    reports[-1]["validated"] = True
    return reports


def _contains_marker(output: list[dict[str, Any]], marker: str) -> bool:
    # Check the requested acknowledgement, not the model's punctuation/Markdown.
    return re.search(r"(?<!\w)" + re.escape(marker) + r"(?!\w)", _text(output)) is not None
