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
    Usage,
)
from ouro.providers.http_transport import (
    HttpxProviderHTTPTransport,
    ProviderHTTPTransport,
)
from ouro.providers.openai_codec import (
    _content_to_text,
    _error_from_response,
    _first_choice,
    _message_to_openai,
    _reasoning_content_to_text,
    _reasoning_field_value,
    _tool_call_deltas,
    _tool_calls_from_message,
    _tool_to_openai,
    _usage_from_response,
    _usage_from_response_or_none,
)
from ouro.providers.reasoning import ReasoningPayloadEncoder
from ouro.providers.retrying import stream_with_retries


class OpenAICompatibleProvider:
    provider_id = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        provider_id: str | None = None,
        endpoint: str = "/chat/completions",
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
            payload = self._chat_completion_payload(
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
            async for event in self._events_from_streaming_chunks(
                chunks,
                model=model,
                supports_tools=model.supports_tools,
            ):
                yield event
            return

        response = await self._post_chat_completion(
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

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        try:
            response = await self._post_chat_completion(
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
            async for event in self._events_from_response(
                response,
                model=model,
                supports_tools=model.supports_tools,
            ):
                if event.type == "error":
                    raise RuntimeError(event.error or "provider connection check failed")
        except Exception as exc:  # noqa: BLE001 - expose check status without crashing callers.
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

    async def _post_chat_completion(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]] | None,
        extra_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = self._chat_completion_payload(
            model=model,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            extra_payload=extra_payload,
        )
        return await self.transport.post_json(
            url=f"{self.base_url}{self.endpoint}",
            headers=self._headers(),
            payload=payload,
            timeout=self.timeout,
        )

    def _chat_completion_payload(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]] | None,
        extra_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        converted_messages = [
            _message_to_openai(
                message,
                supports_vision=model.supports_vision,
                provider_id=self.provider_id,
                model_id=model.model_id,
                include_continuation=model.reasoning.continuation == "text",
            )
            for message in messages
        ]
        if system_prompt:
            converted_messages.insert(0, {"role": "system", "content": system_prompt})
        payload: dict[str, Any] = dict(self.request_options)
        if "response_format" in payload and not model.supports_structured_output:
            raise ValueError("response_format requires an endpoint with structured output support")
        payload["model"] = model.model_id
        payload["messages"] = converted_messages
        if model.temperature is not None:
            payload["temperature"] = model.temperature
        else:
            payload.pop("temperature", None)
        payload["max_tokens"] = model.max_output_tokens
        self.reasoning_encoder.apply(payload, model)
        if tools and model.supports_tools:
            payload["tools"] = [_tool_to_openai(tool) for tool in tools]
        else:
            payload.pop("tools", None)
        payload.update(extra_payload)
        return payload

    def _headers(self) -> dict[str, str]:
        headers = dict(self.default_headers)
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _events_from_response(
        self,
        response: Mapping[str, Any],
        *,
        model: ModelMetadata,
        supports_tools: bool,
    ) -> AsyncIterator[ProviderStreamEvent]:
        response_error = _error_from_response(response)
        if response_error:
            yield ProviderStreamEvent(type="error", error=response_error)
            return

        usage = _usage_from_response(response)
        choice = _first_choice(response)
        if choice is None:
            yield ProviderStreamEvent(type="error", error="provider response has no choices")
            return

        message = choice.get("message") or {}
        if not isinstance(message, Mapping):
            yield ProviderStreamEvent(type="error", error="provider response message is invalid")
            return

        raw_reasoning = _reasoning_field_value(message)
        reasoning = _reasoning_content_to_text(raw_reasoning)
        if model.reasoning.output not in {"summary", "text"}:
            reasoning = ""
        if reasoning:
            yield ProviderStreamEvent(type="reasoning_delta", reasoning_content=reasoning)

        content = _content_to_text(message.get("content"))
        if content:
            yield ProviderStreamEvent(type="text_delta", text=content)

        tool_calls = _tool_calls_from_message(message)
        if tool_calls and not supports_tools:
            yield ProviderStreamEvent(
                type="error",
                error="provider returned tool calls for an endpoint without tool support",
            )
            return
        if not reasoning and not content and not tool_calls:
            finish_reason = choice.get("finish_reason")
            reason = (
                f" finish_reason={finish_reason}"
                if isinstance(finish_reason, str) and finish_reason
                else ""
            )
            yield ProviderStreamEvent(
                type="error",
                error=f"provider returned an empty assistant message{reason}",
            )
            return

        for index, tool_call in enumerate(tool_calls):
            yield ProviderStreamEvent.from_tool_call(tool_call, index=index)
        if tool_calls and reasoning and model.reasoning.continuation == "text":
            yield ProviderStreamEvent(
                type="continuation",
                continuation=ProviderContinuationArtifact(
                    provider_id=self.provider_id,
                    protocol="openai-chat-completions",
                    model_id=model.model_id,
                    blocks=({"reasoning_content": raw_reasoning},),
                ),
            )
        yield ProviderStreamEvent(
            type="done",
            usage=usage,
            completion=_openai_completion(choice.get("finish_reason")),
        )

    async def _events_from_streaming_chunks(
        self,
        chunks: AsyncIterator[Mapping[str, Any]],
        *,
        model: ModelMetadata,
        supports_tools: bool,
    ) -> AsyncIterator[ProviderStreamEvent]:
        usage: Usage | None = None
        saw_content = False
        saw_reasoning = False
        saw_tool_call = False
        continuation_reasoning: list[str] = []
        provider_finish_reason: str | None = None

        async for chunk in chunks:
            response_error = _error_from_response(chunk)
            if response_error:
                yield ProviderStreamEvent(type="error", error=response_error)
                return

            chunk_usage = _usage_from_response_or_none(chunk)
            if chunk_usage is not None:
                usage = chunk_usage

            choice = _first_choice(chunk)
            if choice is None:
                continue
            chunk_finish_reason = choice.get("finish_reason")
            if isinstance(chunk_finish_reason, str) and chunk_finish_reason:
                provider_finish_reason = chunk_finish_reason
            delta = choice.get("delta") or {}
            if not isinstance(delta, Mapping):
                continue

            reasoning = _reasoning_content_to_text(_reasoning_field_value(delta))
            if reasoning:
                if model.reasoning.continuation == "text":
                    continuation_reasoning.append(reasoning)
                if model.reasoning.output in {"summary", "text"}:
                    saw_reasoning = True
                    yield ProviderStreamEvent(type="reasoning_delta", reasoning_content=reasoning)

            content = _content_to_text(delta.get("content"))
            if content:
                saw_content = True
                yield ProviderStreamEvent(type="text_delta", text=content)

            for tool_call_delta in _tool_call_deltas(delta):
                if not supports_tools:
                    yield ProviderStreamEvent(
                        type="error",
                        error="provider returned tool calls for an endpoint without tool support",
                    )
                    return
                saw_tool_call = True
                yield ProviderStreamEvent(
                    type="tool_call_delta",
                    tool_call_delta=tool_call_delta,
                )

        if not saw_content and not saw_reasoning and not saw_tool_call:
            yield ProviderStreamEvent(
                type="error", error="provider returned an empty assistant message"
            )
            return

        if saw_tool_call and continuation_reasoning:
            yield ProviderStreamEvent(
                type="continuation",
                continuation=ProviderContinuationArtifact(
                    provider_id=self.provider_id,
                    protocol="openai-chat-completions",
                    model_id=model.model_id,
                    blocks=({"reasoning_content": "".join(continuation_reasoning)},),
                ),
            )
        yield ProviderStreamEvent(
            type="done",
            usage=usage,
            completion=_openai_completion(provider_finish_reason),
        )


def _openai_completion(value: object) -> ProviderCompletion:
    provider_reason = value if isinstance(value, str) and value else None
    mapping: dict[str, ProviderFinishReason] = {
        "stop": "stop",
        "length": "length",
        "tool_calls": "tool_call",
        "function_call": "tool_call",
        "content_filter": "content_filter",
    }
    finish_reason = (
        mapping.get(provider_reason, "other") if provider_reason is not None else "unknown"
    )
    return ProviderCompletion(
        finish_reason=finish_reason,
        provider_finish_reason=provider_reason,
    )
