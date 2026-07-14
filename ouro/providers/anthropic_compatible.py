from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from ouro.core.messages import MessageRecord
from ouro.core.provider import (
    ModelMetadata,
    ProviderCheckResult,
    ProviderRetryPolicy,
    ProviderStreamEvent,
    ToolCallDelta,
    Usage,
)
from ouro.providers.anthropic_codec import (
    content_blocks_from_response,
    error_from_anthropic,
    merge_usage,
    messages_to_anthropic,
    tool_to_anthropic,
    usage_from_anthropic,
)
from ouro.providers.http_transport import HttpxProviderHTTPTransport, ProviderHTTPTransport
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
        messages: list[MessageRecord],
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
                    MessageRecord.text(
                        session_id="connection_check",
                        role="user",
                        text="ping",
                    )
                ],
                tools=None,
                extra_payload={"stream": False, "max_tokens": 1},
            )
            events = self._events_from_response(response, supports_tools=model.supports_tools)
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
        messages: list[MessageRecord],
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
                chunks, supports_tools=model.supports_tools
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
            supports_tools=model.supports_tools,
        ):
            yield event

    async def _post_message(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str,
        messages: list[MessageRecord],
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
        messages: list[MessageRecord],
        tools: list[dict[str, Any]] | None,
        extra_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        system, converted_messages = messages_to_anthropic(
            messages,
            system_prompt=system_prompt,
            supports_vision=model.supports_vision,
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
        level = model.default_thinking_level
        if model.supports_reasoning and level not in {None, "off"}:
            output_config = payload.get("output_config")
            output_config = dict(output_config) if isinstance(output_config, Mapping) else {}
            output_config["effort"] = "low" if level == "minimal" else level
            payload["output_config"] = output_config
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
        if not text_parts and not reasoning_parts and not tool_calls:
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
        for tool_call in tool_calls:
            yield ProviderStreamEvent(type="tool_call", tool_call=tool_call)
        yield ProviderStreamEvent(type="done", usage=usage_from_anthropic(response.get("usage")))

    async def _events_from_stream(
        self,
        chunks: AsyncIterator[Mapping[str, Any]],
        *,
        supports_tools: bool,
    ) -> AsyncIterator[ProviderStreamEvent]:
        usage = Usage()
        saw_content = False
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
                        yield ProviderStreamEvent(
                            type="tool_call_delta",
                            tool_call_delta=ToolCallDelta(
                                index=index,
                                id_delta=call_id,
                                name_delta=name,
                            ),
                        )
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
                        yield ProviderStreamEvent(
                            type="reasoning_delta",
                            reasoning_content=thinking,
                        )
                elif (
                    delta_type == "input_json_delta"
                    and isinstance(index, int)
                    and isinstance(delta.get("partial_json"), str)
                    and delta.get("partial_json")
                ):
                    saw_content = True
                    yield ProviderStreamEvent(
                        type="tool_call_delta",
                        tool_call_delta=ToolCallDelta(
                            index=index,
                            arguments_delta=str(delta["partial_json"]),
                        ),
                    )
            elif event_type == "message_delta":
                usage = merge_usage(usage, chunk.get("usage"))
            elif event_type == "message_stop":
                if not saw_content:
                    yield ProviderStreamEvent(
                        type="error",
                        error="provider returned an empty assistant message",
                    )
                    return
                yield ProviderStreamEvent(type="done", usage=usage)
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
