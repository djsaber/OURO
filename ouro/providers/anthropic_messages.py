from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import aclosing
from copy import deepcopy
from typing import Any

from ouro.auth.request_auth import RequestAuthenticator, StaticRequestAuthenticator
from ouro.config.provider_headers import ProviderHeaderValue
from ouro.core.messages import ModelMessage, ProviderContinuationArtifact
from ouro.core.provider import (
    ModelMetadata,
    ProviderCheckResult,
    ProviderCompletion,
    ProviderFinishReason,
    ProviderRetryPolicy,
    ProviderStreamEvent,
    ToolCallDelta,
    Usage,
)
from ouro.core.request_context import ProviderRequestContext
from ouro.providers.anthropic_messages_codec import (
    content_blocks_from_response,
    continuation_blocks_from_response,
    error_from_anthropic,
    merge_usage,
    messages_to_anthropic_messages,
    tool_to_anthropic_messages,
    usage_from_anthropic,
)
from ouro.providers.authentication import (
    ProviderRequestPolicy,
    request_authentication,
    safe_response_event,
)
from ouro.providers.connection_check import check_connection_events
from ouro.providers.headers import ProviderHeaderResolver
from ouro.providers.http_transport import HttpxProviderHTTPTransport, ProviderHTTPTransport
from ouro.providers.reasoning import ReasoningPayloadEncoder
from ouro.providers.request_context import model_request_context
from ouro.providers.retrying import stream_with_retries


class AnthropicMessagesProvider:
    """Adapter for Anthropic's Messages wire protocol and compatible gateways."""

    provider_id = "anthropic-messages"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        authenticator: RequestAuthenticator | None = None,
        request_policy: ProviderRequestPolicy | None = None,
        provider_id: str | None = None,
        api_path: str = "/v1/messages",
        timeout: float = 60.0,
        transport: ProviderHTTPTransport | None = None,
        default_headers: Mapping[str, str] | None = None,
        headers: Mapping[str, ProviderHeaderValue] | None = None,
        request_options: Mapping[str, Any] | None = None,
        reasoning_encoder: ReasoningPayloadEncoder | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
    ) -> None:
        self.provider_id = provider_id or self.provider_id
        self.base_url = base_url.rstrip("/")
        self.api_path = api_path if api_path.startswith("/") else f"/{api_path}"
        if authenticator is not None and api_key is not None:
            raise ValueError("provide either an API Key or a request authenticator")
        self.api_key = api_key
        self.authenticator = authenticator or StaticRequestAuthenticator(
            {"x-api-key": api_key} if api_key else {}
        )
        self.request_policy = request_policy or ProviderRequestPolicy()
        if not self.request_policy.supports_output_limit:
            raise ValueError("Messages protocol requires a request output limit")
        self.timeout = timeout
        self.transport = transport or HttpxProviderHTTPTransport()
        self.default_headers = dict(default_headers or {})
        self.header_resolver = ProviderHeaderResolver(headers)
        self.redact_errors = bool(headers)
        self.request_options = dict(request_options or {})
        self.reasoning_encoder = reasoning_encoder or ReasoningPayloadEncoder(None)
        self.retry_policy = retry_policy or ProviderRetryPolicy()

    async def close(self) -> None:
        close = getattr(self.transport, "aclose", None)
        if close is not None:
            await close()

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, Any]] | None = None,
        request_context: ProviderRequestContext | None = None,
    ) -> AsyncGenerator[ProviderStreamEvent]:
        request_context = model_request_context(model, request_context)
        headers = self.header_resolver.resolve(request_context, defaults=self._headers())
        attempts = stream_with_retries(
            lambda: self._stream_once(
                model=model,
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                headers=headers,
            ),
            self.retry_policy,
            include_reasoning=model.reasoning.output in {"summary", "text"},
        )
        async with aclosing(attempts):
            async for event in attempts:
                yield event

    async def check_connection(
        self, model: ModelMetadata, *, request_context: ProviderRequestContext | None = None
    ) -> ProviderCheckResult:
        request_context = model_request_context(model, request_context, purpose="connection_check")
        check_model = model.model_copy(update={"supports_streaming": False, "max_output_tokens": 1})
        return await check_connection_events(
            self.stream(
                model=check_model,
                messages=[ModelMessage.text(role="user", text="ping")],
                request_context=request_context,
            ),
            provider_id=self.provider_id,
            model=model,
        )

    async def _stream_once(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]] | None,
        headers: Mapping[str, str],
    ) -> AsyncGenerator[ProviderStreamEvent]:
        async with request_authentication(
            self.authenticator,
            url=f"{self.base_url}{self.api_path}",
            headers=headers,
            redact_errors=self.redact_errors,
            policy=self.request_policy,
        ) as snapshot:
            events = self._stream_response(
                model=model,
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                headers=snapshot.headers,
            )
            async with aclosing(events):
                async for event in events:
                    yield safe_response_event(event, snapshot, redact_errors=self.redact_errors)

    async def _stream_response(
        self,
        *,
        headers: Mapping[str, str],
        model: ModelMetadata,
        system_prompt: str,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]] | None,
    ) -> AsyncGenerator[ProviderStreamEvent]:
        stream_json = getattr(self.transport, "stream_json", None)
        if model.supports_streaming and callable(stream_json):
            payload = self._message_payload(
                model=model,
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                extra_payload={"stream": True},
            )
            chunks = stream_json(
                url=f"{self.base_url}{self.api_path}",
                headers=headers,
                payload=payload,
                timeout=self.timeout,
            )
            async with aclosing(chunks):
                events = self._events_from_stream(
                    chunks, model=model, supports_tools=model.supports_tools
                )
                async with aclosing(events):
                    async for event in events:
                        yield event
            return

        response = await self._post_message(
            headers=headers,
            model=model,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            extra_payload={"stream": False},
        )
        async for event in self._events_from_response(
            response,
            model=model,
            supports_tools=model.supports_tools,
        ):
            yield event

    async def _post_message(
        self,
        *,
        headers: Mapping[str, str],
        model: ModelMetadata,
        system_prompt: str,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]] | None,
        extra_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self.transport.post_json(
            url=f"{self.base_url}{self.api_path}",
            headers=headers,
            payload=self._message_payload(
                model=model,
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                extra_payload=extra_payload,
            ),
            timeout=self.timeout,
        )

    def _message_payload(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]] | None,
        extra_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if model.max_output_tokens is None:
            raise ValueError("Messages protocol requires an explicit max_output_tokens")
        system, converted_messages = messages_to_anthropic_messages(
            messages,
            system_prompt=system_prompt,
            supports_image_input=model.supports_image_input,
            provider_id=self.provider_id,
            model_id=model.model_id,
            include_continuation=model.reasoning.continuation == "opaque",
        )
        payload: dict[str, Any] = dict(self.request_options)
        payload.update(
            {
                "model": model.model_id,
                "messages": converted_messages,
                "max_tokens": model.max_output_tokens,
            }
        )
        if system:
            payload["system"] = system
        else:
            payload.pop("system", None)
        if model.temperature is not None:
            payload["temperature"] = model.temperature
        else:
            payload.pop("temperature", None)
        self.reasoning_encoder.apply(payload, model)
        thinking = payload.get("thinking")
        if isinstance(thinking, dict) and "display" in thinking:
            if thinking.get("type") not in {"adaptive", "enabled"}:
                raise ValueError("summary requests require an enabled Messages thinking profile")
        if tools and model.supports_tools:
            payload["tools"] = [tool_to_anthropic_messages(tool) for tool in tools]
        else:
            payload.pop("tools", None)
        payload.update(extra_payload)
        return payload

    def _headers(self) -> dict[str, str]:
        return {"anthropic-version": "2023-06-01", **self.default_headers}

    async def _events_from_response(
        self,
        response: Mapping[str, Any],
        *,
        model: ModelMetadata,
        supports_tools: bool,
    ) -> AsyncIterator[ProviderStreamEvent]:
        response_error = error_from_anthropic(response)
        if response_error:
            yield ProviderStreamEvent(type="error", error=response_error)
            return

        text_parts, reasoning_parts, tool_calls = content_blocks_from_response(response)
        if tool_calls and not supports_tools:
            yield ProviderStreamEvent(
                type="error",
                error="provider returned tool calls for an endpoint without tool support",
            )
            return
        for reasoning in reasoning_parts:
            yield ProviderStreamEvent(type="reasoning_delta", reasoning_content=reasoning)
        for text in text_parts:
            yield ProviderStreamEvent(type="text_delta", text=text)
        for index, tool_call in enumerate(tool_calls):
            yield ProviderStreamEvent.from_tool_call(tool_call, index=index)
        continuation_blocks = (
            continuation_blocks_from_response(response)
            if model.reasoning.continuation == "opaque"
            else ()
        )
        if continuation_blocks:
            yield ProviderStreamEvent(
                type="continuation",
                continuation=ProviderContinuationArtifact(
                    provider_id=self.provider_id,
                    protocol="anthropic-messages",
                    model_id=model.model_id,
                    blocks=continuation_blocks,
                ),
            )
        yield ProviderStreamEvent(
            type="done",
            usage=usage_from_anthropic(response.get("usage")),
            completion=_anthropic_messages_completion(
                response.get("stop_reason"),
                has_reasoning=any(
                    block.get("type") in {"thinking", "redacted_thinking"}
                    for block in (response.get("content") or [])
                    if isinstance(block, Mapping)
                ),
            ),
        )

    async def _events_from_stream(
        self,
        chunks: AsyncIterator[Mapping[str, Any]],
        *,
        model: ModelMetadata,
        supports_tools: bool,
    ) -> AsyncGenerator[ProviderStreamEvent]:
        usage = Usage()
        saw_reasoning = False
        continuation_blocks: dict[int, dict[str, Any]] = {}
        content_fragments: dict[tuple[int, str], list[str]] = {}
        tool_inputs: dict[int, list[str]] = {}
        tool_call_indices: dict[int, int] = {}
        provider_finish_reason: str | None = None
        async for chunk in chunks:
            response_error = error_from_anthropic(chunk)
            if response_error:
                yield ProviderStreamEvent(type="error", error=response_error)
                return

            event_type = chunk.get("type")
            if event_type == "message_start":
                message = chunk.get("message")
                if isinstance(message, Mapping):
                    usage = merge_usage(usage, message.get("usage"))
            elif event_type == "content_block_start":
                block = chunk.get("content_block")
                index = chunk.get("index")
                if not isinstance(block, Mapping) or not isinstance(index, int):
                    continue
                if model.reasoning.continuation == "opaque":
                    continuation_blocks[index] = deepcopy(dict(block))
                if block.get("type") == "tool_use":
                    if not supports_tools:
                        yield ProviderStreamEvent(
                            type="error",
                            error=(
                                "provider returned tool calls for an endpoint without tool support"
                            ),
                        )
                        return
                    name = block.get("name")
                    call_id = block.get("id")
                    if isinstance(name, str) and name and isinstance(call_id, str) and call_id:
                        tool_call_index = tool_call_indices.setdefault(
                            index,
                            len(tool_call_indices),
                        )
                        yield ProviderStreamEvent(
                            type="tool_call_delta",
                            tool_call_delta=ToolCallDelta(
                                index=tool_call_index,
                                id_delta=call_id,
                                name_delta=name,
                            ),
                        )
                elif block.get("type") in {"thinking", "redacted_thinking"}:
                    saw_reasoning = True
            elif event_type == "content_block_delta":
                delta = chunk.get("delta")
                index = chunk.get("index")
                if not isinstance(delta, Mapping):
                    continue
                delta_type = delta.get("type")
                if delta_type == "text_delta" and isinstance(delta.get("text"), str):
                    text = str(delta["text"])
                    if text:
                        yield ProviderStreamEvent(type="text_delta", text=text)
                    if isinstance(index, int) and index in continuation_blocks:
                        content_fragments.setdefault((index, "text"), []).append(text)
                elif delta_type == "thinking_delta" and isinstance(delta.get("thinking"), str):
                    thinking = str(delta["thinking"])
                    if thinking:
                        saw_reasoning = True
                        yield ProviderStreamEvent(
                            type="reasoning_delta",
                            reasoning_content=thinking,
                        )
                    if isinstance(index, int) and index in continuation_blocks:
                        content_fragments.setdefault((index, "thinking"), []).append(thinking)
                elif delta_type == "signature_delta" and isinstance(delta.get("signature"), str):
                    if isinstance(index, int) and index in continuation_blocks:
                        content_fragments.setdefault((index, "signature"), []).append(
                            str(delta["signature"])
                        )
                elif delta_type == "input_json_delta" and isinstance(
                    delta.get("partial_json"), str
                ):
                    if not isinstance(index, int) or index not in tool_call_indices:
                        yield ProviderStreamEvent(
                            type="error",
                            error=(
                                "provider streamed tool input for an unknown content block "
                                f"index: {index!r}"
                            ),
                        )
                        return
                    if not delta.get("partial_json"):
                        continue
                    if index in continuation_blocks:
                        tool_inputs.setdefault(index, []).append(str(delta["partial_json"]))
                    yield ProviderStreamEvent(
                        type="tool_call_delta",
                        tool_call_delta=ToolCallDelta(
                            index=tool_call_indices[index],
                            arguments_delta=str(delta["partial_json"]),
                        ),
                    )
                elif delta_type == "citations_delta" and isinstance(delta.get("citation"), Mapping):
                    if isinstance(index, int) and index in continuation_blocks:
                        continuation_blocks[index].setdefault("citations", []).append(
                            deepcopy(dict(delta["citation"]))
                        )
            elif event_type == "message_delta":
                usage = merge_usage(usage, chunk.get("usage"))
                delta = chunk.get("delta")
                if isinstance(delta, Mapping):
                    stop_reason = delta.get("stop_reason")
                    if isinstance(stop_reason, str) and stop_reason:
                        provider_finish_reason = stop_reason
            elif event_type == "message_stop":
                completion = _anthropic_messages_completion(
                    provider_finish_reason, has_reasoning=saw_reasoning
                )
                if continuation_blocks and completion.finish_reason in {
                    "stop",
                    "tool_call",
                    "unknown",
                }:
                    for (index, field), fragments in content_fragments.items():
                        initial = str(continuation_blocks[index].get(field) or "")
                        continuation_blocks[index][field] = initial + "".join(fragments)
                    for index, fragments in tool_inputs.items():
                        continuation_blocks[index]["input"] = json.loads("".join(fragments))
                    yield ProviderStreamEvent(
                        type="continuation",
                        continuation=ProviderContinuationArtifact(
                            provider_id=self.provider_id,
                            protocol="anthropic-messages",
                            model_id=model.model_id,
                            blocks=continuation_blocks_from_response(
                                {
                                    "content": [
                                        continuation_blocks[index]
                                        for index in sorted(continuation_blocks)
                                    ]
                                }
                            ),
                        ),
                    )
                yield ProviderStreamEvent(
                    type="done",
                    usage=usage,
                    completion=completion,
                )
                return

        yield ProviderStreamEvent(
            type="error",
            error="provider stream ended without message_stop",
        )


def _anthropic_messages_completion(
    value: object, *, has_reasoning: bool = False
) -> ProviderCompletion:
    provider_reason = value if isinstance(value, str) and value else None
    mapping: dict[str, ProviderFinishReason] = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_call",
        "refusal": "content_filter",
    }
    finish_reason = (
        mapping.get(provider_reason, "other") if provider_reason is not None else "unknown"
    )
    return ProviderCompletion(
        finish_reason=finish_reason,
        provider_finish_reason=provider_reason,
        has_reasoning=has_reasoning,
    )
