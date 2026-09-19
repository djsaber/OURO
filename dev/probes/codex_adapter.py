"""Memory-only real-service verification through the production Responses adapter."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing
from typing import Any, cast

import httpx

from dev.probes.codex_oauth import CLIENT_ID, ProbeError
from dev.probes.codex_responses import RESPONSES_URL
from ouro.auth.contracts import AuthError, CredentialGrant, CredentialRecord
from ouro.auth.drivers.openai_codex import OpenAICodexDriver
from ouro.auth.request_auth import RequestAuthSnapshot
from ouro.core.messages import ModelMessage, ToolResultReference
from ouro.core.provider import ModelMetadata, ProviderRetryPolicy
from ouro.core.tools import ToolCallRequest
from ouro.providers.authentication import ProviderRequestPolicy
from ouro.providers.http_transport import HttpxProviderHTTPTransport, ProviderTransportError
from ouro.providers.openai_responses import OpenAIResponsesProvider
from ouro.providers.responses_codec import tool_arguments

CODEX_REQUEST_POLICY = ProviderRequestPolicy(
    billing_mode="subscription",
    supports_output_limit=False,
)


def _request_error(body: object) -> dict[str, str]:
    """Export fixed vocabulary only, never redact and print arbitrary service text."""
    if not isinstance(body, dict):
        return {}
    parameters = {
        "model",
        "input",
        "instructions",
        "stream",
        "store",
        "include",
        "truncation",
        "max_output_tokens",
        "temperature",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "reasoning",
        "reasoning.effort",
        "text",
        "text.format",
    }
    reasons = {
        "unsupported_parameter",
        "unknown_parameter",
        "invalid_parameter",
        "invalid_value",
        "missing_required_parameter",
        "invalid_request_error",
    }
    error = body.get("error")
    details: dict[str, str] = {}
    if isinstance(error, dict):
        parameter, reason = error.get("param"), error.get("code")
        if isinstance(parameter, str) and parameter in parameters:
            details["parameter"] = parameter
        if isinstance(reason, str) and reason in reasons:
            details["reason"] = reason
        message = error.get("message")
    else:
        message = error
    if details:
        return details
    # Some services return a plain detail instead of param/code. Recognize the
    # entire fixed sentence; merely mentioning a field is not evidence of rejection.
    for text in (message, body.get("detail")):
        if not isinstance(text, str):
            continue
        for parameter in parameters:
            if text in {
                f"Unsupported parameter: {parameter}",
                f"Unsupported parameter: '{parameter}'",
            }:
                return {"parameter": parameter, "reason": "unsupported_parameter"}
    return {}


class ProbeAuthentication:
    """Inject driver-encoded snapshots without a disk store or implicit refresh."""

    def __init__(self, client: httpx.AsyncClient, grant: CredentialGrant) -> None:
        self.driver = OpenAICodexDriver(client_id=CLIENT_ID, client=client)
        self.replace(grant, revision=1)

    def replace(self, grant: CredentialGrant, *, revision: int) -> None:
        record = CredentialRecord(
            credential_id="cred_" + "0" * 32,
            driver_id=self.driver.descriptor.driver_id,
            revision=revision,
            state="active",
            **grant.model_dump(),
        )
        material = self.driver.encode(record)
        self.snapshot = RequestAuthSnapshot(
            headers=material.headers,
            revision=revision,
            scope=("codex-probe", *material.account_scope),
        )

    async def acquire(self, url: str) -> RequestAuthSnapshot:
        if url != RESPONSES_URL:
            raise AuthError("authentication_target_rejected")
        return self.snapshot

    async def recover(self, snapshot: RequestAuthSnapshot) -> bool:
        return False


class ProbeTransport(HttpxProviderHTTPTransport):
    """Observe safe rejection facts without changing production transport behavior."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        super().__init__(client)
        self._probe_client = client
        self.status: int | None = None
        self.request_error: dict[str, str] = {}

    async def _observe_rejection(self, response: httpx.Response) -> None:
        if response.status_code not in {400, 422} or str(response.request.url) != RESPONSES_URL:
            return
        await response.aread()
        try:
            body = response.json()
        except ValueError:
            return
        self.request_error = _request_error(body)

    async def stream_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> AsyncGenerator[Mapping[str, Any]]:
        self.status = None
        self.request_error = {}
        hooks = self._probe_client.event_hooks["response"]
        hooks.append(self._observe_rejection)
        try:
            # The concrete transport returns an async generator; its public
            # annotation exposes only the iteration contract.
            source = cast(
                AsyncGenerator[Mapping[str, Any]],
                super().stream_json(
                    url=url,
                    headers=headers,
                    payload=payload,
                    timeout=timeout,
                ),
            )
            async with aclosing(source) as chunks:
                async for chunk in chunks:
                    yield chunk
        except ProviderTransportError as error:
            self.status = error.status_code
            raise
        finally:
            hooks.remove(self._observe_rejection)


async def _request(
    provider: OpenAIResponsesProvider,
    transport: ProbeTransport,
    model: ModelMetadata,
    history: list[ModelMessage],
    *,
    scenario: str,
    observations: list[dict[str, Any]],
    tool_round: int | None = None,
) -> ModelMessage:
    counts: dict[str, int] = {}
    report: dict[str, Any] = {
        "scenario": scenario,
        "validated": False,
        "canonical_event_counts": counts,
    }
    observations.append(report)
    tools = None
    provider.request_options = {}
    if tool_round is not None:
        tools = [
            {
                "name": "ouro_probe_echo",
                "description": "Return the verification round number.",
                "parameters_schema": {
                    "type": "object",
                    "properties": {"round": {"type": "integer"}},
                    "required": ["round"],
                    "additionalProperties": False,
                },
            }
        ]
        provider.request_options = {
            "tool_choice": {"type": "function", "name": "ouro_probe_echo"},
            "parallel_tool_calls": False,
        }
    text = ""
    artifact = None
    done = False
    async with aclosing(
        provider.stream(
            model=model,
            system_prompt="Follow the user's verification instructions precisely.",
            messages=history,
            tools=tools,
        )
    ) as events:
        async for event in events:
            counts[event.type] = counts.get(event.type, 0) + 1
            if event.type == "error":
                report["request_error"] = dict(transport.request_error)
                raise ProbeError(
                    event.error_code or "adapter_request_failed", status=transport.status
                )
            if event.type == "text_delta":
                text += event.text or ""
            elif event.type == "continuation":
                artifact = event.continuation
            elif event.type == "done":
                done = True
                if event.usage is None:
                    raise ProbeError("missing_usage")
                report["usage"] = event.usage.model_dump(exclude_none=True)
    if not done or artifact is None:
        raise ProbeError("missing_continuation")
    items = artifact.blocks[0]["items"]
    report.update(
        text_characters=len(text),
        encrypted_reasoning=any(
            item.get("type") == "reasoning" and bool(item.get("encrypted_content"))
            for item in items
        ),
        item_types=[item["type"] for item in items],
    )
    calls = [
        ToolCallRequest(
            id=item["call_id"], name=item["name"], arguments=tool_arguments(item["arguments"])
        )
        for item in items
        if item["type"] == "function_call"
    ]
    if tool_round is not None:
        if len(calls) != 1 or calls[0].name != "ouro_probe_echo":
            raise ProbeError("unexpected_tool_identity")
        if (
            calls[0].arguments != {"round": tool_round}
            or type(calls[0].arguments["round"]) is not int
        ):
            raise ProbeError("unexpected_tool_arguments")
    else:
        marker = "OURO_OK" if scenario == "adapter_text" else "OURO_TOOLS_OK"
        if calls or re.search(r"(?<!\w)" + marker + r"(?!\w)", text) is None:
            raise ProbeError("unexpected_text_result")
    report["validated"] = True
    return ModelMessage.text(role="assistant", text=text).model_copy(
        update={"tool_calls": calls, "provider_continuation": artifact}
    )


async def verify_adapter(
    client: httpx.AsyncClient,
    grant: CredentialGrant,
    *,
    model: str,
    max_output_tokens: int,
    refresh: Callable[[CredentialGrant], Awaitable[CredentialGrant]],
    observations: list[dict[str, Any]],
) -> None:
    auth = ProbeAuthentication(client, grant)
    transport = ProbeTransport(client)
    provider = OpenAIResponsesProvider(
        base_url=RESPONSES_URL.removesuffix("/responses"),
        authenticator=auth,
        request_policy=CODEX_REQUEST_POLICY,
        transport=transport,
        default_headers={"originator": "ouro-auth-probe"},
        retry_policy=ProviderRetryPolicy(max_attempts=1),
    )
    metadata = ModelMetadata(
        model_id=model,
        provider_id=provider.provider_id,
        max_output_tokens=max_output_tokens,
        supports_tools=True,
        supports_streaming=True,
    )
    history = [ModelMessage.text(role="user", text="Reply with exactly OURO_OK.")]
    try:
        first = await _request(
            provider,
            transport,
            metadata,
            history,
            scenario="adapter_text",
            observations=observations,
        )
        history.append(first)
        scope = auth.snapshot.scope
        auth.replace(await refresh(grant), revision=2)
        if auth.snapshot.scope != scope:
            raise ProbeError("continuation_scope_mismatch")
        seen_calls: set[str] = set()
        for number in (1, 2):
            history.append(
                ModelMessage.text(
                    role="user", text=f"Call ouro_probe_echo exactly once with round={number}."
                )
            )
            answer = await _request(
                provider,
                transport,
                metadata,
                history,
                scenario=f"adapter_tool_{number}",
                observations=observations,
                tool_round=number,
            )
            call = answer.tool_calls[0]
            if call.id in seen_calls:
                observations[-1]["validated"] = False
                raise ProbeError("unexpected_tool_identity")
            seen_calls.add(call.id)
            history.append(answer)
            history.append(
                ModelMessage.text(
                    role="tool_result", text=json.dumps({"round": number, "ok": True})
                ).model_copy(
                    update={
                        "tool_result": ToolResultReference(
                            tool_call_id=call.id,
                            tool_name=call.name,
                        )
                    }
                )
            )
        history.append(
            ModelMessage.text(
                role="user", text="If both tool results are ok, reply exactly OURO_TOOLS_OK."
            )
        )
        await _request(
            provider,
            transport,
            metadata,
            history,
            scenario="adapter_continuation",
            observations=observations,
        )
        observations[-1]["scope_stable_after_refresh"] = True
        observations[-1]["encrypted_reasoning_replayed"] = observations[0]["encrypted_reasoning"]
    finally:
        await provider.close()
