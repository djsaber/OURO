from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

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
from ouro.providers.anthropic_codec import (
    content_blocks_from_response,
    continuation_blocks_from_response,
    error_from_anthropic,
    merge_usage,
    messages_to_anthropic,
    tool_to_anthropic,
    usage_from_anthropic,
)
from ouro.providers.http_transport import HttpxProviderHTTPTransport, ProviderHTTPTransport
from ouro.providers.reasoning import ReasoningPayloadEncoder
from ouro.providers.retrying import stream_with_retries


class AnthropicCompatibleProvider:
    """Adapter for Anthropic's Messages wire protocol and compatible gateways."""

    provider_id = "anthropic-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        provider_id: str | None = None,
        endpoint: str = "/v1/messages",
        timeout: float = 60.0,
        transport: ProviderHTTPTransport | None = None,
        default_headers: Mapping[str, str] | None = None,
        request_options: Mapping[str, Any] | None = None,
        reasoning_encoder: ReasoningPayloadEncoder | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
    ) -> None:
        self.provider_id = provider_id or self.provider_id
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        self.api_key = api_key
        self.timeout = timeout
        self.transport = transport or HttpxProviderHTTPTransport()
        self.default_headers = dict(default_headers or {})
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
    ) -> AsyncIterator[ProviderStreamEvent]:
        async for event in stream_with_retries(
            lambda: self._stream_once(
                model=model,
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
            ),
            self.retry_policy,
        ):
            yield event

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        try:
            response = await self._post_message(
                model=model,
                system_prompt="",
                messages=[
                    ModelMessage.text(
                        role="user",
                        text="ping",
                    )
                ],
                tools=None,
                extra_payload={"stream": False, "max_tokens": 1},
            )
            events = self._events_from_response(
                response,
                model=model,
                supports_tools=model.supports_tools,
            )
            async for event in events:
                if event.type == "error":
                    raise RuntimeError(event.error or "provider connection check failed")
        except Exception as exc:  # noqa: BLE001 - checks return typed failure status.
            return ProviderCheckResult(
                provider_id=self.provider_id,
                ok=False,
                model_id=model.model_id,
                message=str(exc),
                metadata={"endpoint_id": model.endpoint_id} if model.endpoint_id else {},
            )
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
            metadata={"endpoint_id": model.endpoint_id} if model.endpoint_id else {},
        )

    async def _stream_once(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]] | None,
    ) -> AsyncIterator[ProviderStreamEvent]:
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
                url=f"{self.base_url}{self.endpoint}",
                headers=self._headers(),
                payload=payload,
                timeout=self.timeout,
            )
            async for event in self._events_from_stream(
                chunks,
                model=model,
                supports_tools=model.supports_tools,
            ):
                yield event
            return

        response = await self._post_message(
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
        model: ModelMetadata,
        system_prompt: str,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]] | None,
        extra_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self.transport.post_json(
            url=f"{self.base_url}{self.endpoint}",
            headers=self._headers(),
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
        system, converted_messages = messages_to_anthropic(
            messages,
            system_prompt=system_prompt,
            supports_vision=model.supports_vision,
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
        if tools and model.supports_tools:
            payload["tools"] = [tool_to_anthropic(tool) for tool in tools]
        else:
            payload.pop("tools", None)
        payload.update(extra_payload)
        return payload

    def _headers(self) -> dict[str, str]:
        headers = {"anthropic-version": "2023-06-01", **self.default_headers}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

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
        continuation_blocks = continuation_blocks_from_response(response)
        if model.reasoning.output not in {"summary", "text"}:
            reasoning_parts = []
        if tool_calls and not supports_tools:
            yield ProviderStreamEvent(
                type="error",
                error="provider returned tool calls for an endpoint without tool support",
            )
            return
        if not text_parts and not reasoning_parts and not tool_calls and not continuation_blocks:
            stop_reason = response.get("stop_reason")
            suffix = f" stop_reason={stop_reason}" if isinstance(stop_reason, str) else ""
            yield ProviderStreamEvent(
                type="error",
                error=f"provider returned an empty assistant message{suffix}",
            )
            return
        for reasoning in reasoning_parts:
            yield ProviderStreamEvent(type="reasoning_delta", reasoning_content=reasoning)
        for text in text_parts:
            yield ProviderStreamEvent(type="text_delta", text=text)
        for index, tool_call in enumerate(tool_calls):
            yield ProviderStreamEvent.from_tool_call(tool_call, index=index)
        if tool_calls and continuation_blocks and model.reasoning.continuation == "opaque":
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
            completion=_anthropic_completion(response.get("stop_reason")),
        )

    async def _events_from_stream(
        self,
        chunks: AsyncIterator[Mapping[str, Any]],
        *,
        model: ModelMetadata,
        supports_tools: bool,
    ) -> AsyncIterator[ProviderStreamEvent]:
        usage = Usage()
        saw_content = False
        saw_tool_call = False
        continuation_blocks: dict[int, dict[str, Any]] = {}
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
                        saw_content = True
                        saw_tool_call = True
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
                    if model.reasoning.continuation == "opaque":
                        continuation_blocks[index] = dict(block)
                    saw_content = True
            elif event_type == "content_block_delta":
                delta = chunk.get("delta")
                index = chunk.get("index")
                if not isinstance(delta, Mapping):
                    continue
                delta_type = delta.get("type")
                if delta_type == "text_delta" and isinstance(delta.get("text"), str):
                    text = str(delta["text"])
                    if text:
                        saw_content = True
                        yield ProviderStreamEvent(type="text_delta", text=text)
                elif delta_type == "thinking_delta" and isinstance(delta.get("thinking"), str):
                    thinking = str(delta["thinking"])
                    if thinking:
                        saw_content = True
                        if model.reasoning.output in {"summary", "text"}:
                            yield ProviderStreamEvent(
                                type="reasoning_delta",
                                reasoning_content=thinking,
                            )
                    if isinstance(index, int) and index in continuation_blocks:
                        current = str(continuation_blocks[index].get("thinking") or "")
                        continuation_blocks[index]["thinking"] = current + thinking
                elif delta_type == "signature_delta" and isinstance(delta.get("signature"), str):
                    if isinstance(index, int) and index in continuation_blocks:
                        current = str(continuation_blocks[index].get("signature") or "")
                        continuation_blocks[index]["signature"] = current + str(delta["signature"])
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
                    saw_content = True
                    yield ProviderStreamEvent(
                        type="tool_call_delta",
                        tool_call_delta=ToolCallDelta(
                            index=tool_call_indices[index],
                            arguments_delta=str(delta["partial_json"]),
                        ),
                    )
            elif event_type == "message_delta":
                usage = merge_usage(usage, chunk.get("usage"))
                delta = chunk.get("delta")
                if isinstance(delta, Mapping):
                    stop_reason = delta.get("stop_reason")
                    if isinstance(stop_reason, str) and stop_reason:
                        provider_finish_reason = stop_reason
            elif event_type == "message_stop":
                if not saw_content:
                    yield ProviderStreamEvent(
                        type="error",
                        error="provider returned an empty assistant message",
                    )
                    return
                if saw_tool_call and continuation_blocks:
                    yield ProviderStreamEvent(
                        type="continuation",
                        continuation=ProviderContinuationArtifact(
                            provider_id=self.provider_id,
                            protocol="anthropic-messages",
                            model_id=model.model_id,
                            blocks=tuple(
                                continuation_blocks[index] for index in sorted(continuation_blocks)
                            ),
                        ),
                    )
                yield ProviderStreamEvent(
                    type="done",
                    usage=usage,
                    completion=_anthropic_completion(provider_finish_reason),
                )
                return

        if not saw_content:
            yield ProviderStreamEvent(
                type="error",
                error="provider returned an empty assistant message",
            )
        else:
            yield ProviderStreamEvent(
                type="error",
                error="provider stream ended without message_stop",
            )


def _anthropic_completion(value: object) -> ProviderCompletion:
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
    )
