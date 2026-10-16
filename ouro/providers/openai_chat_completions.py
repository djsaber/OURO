from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import aclosing
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
    Usage,
)
from ouro.core.request_context import ProviderRequestContext
from ouro.providers.authentication import (
    ProviderRequestPolicy,
    request_authentication,
    safe_response_event,
)
from ouro.providers.connection_check import check_connection_events
from ouro.providers.headers import ProviderHeaderResolver
from ouro.providers.http_transport import (
    HttpxProviderHTTPTransport,
    ProviderHTTPTransport,
)
from ouro.providers.openai_chat_completions_codec import (
    _content_to_text,
    _error_from_response,
    _first_choice,
    _reasoning_content_to_text,
    _reasoning_field_value,
    _tool_call_deltas,
    _tool_to_chat_completions,
    _usage_from_response,
    _usage_from_response_or_none,
    messages_to_chat_completions,
    reasoning_fields_from_response,
)
from ouro.providers.reasoning import ReasoningPayloadEncoder
from ouro.providers.request_context import model_request_context
from ouro.providers.retrying import stream_with_retries


class OpenAIChatCompletionsProvider:
    provider_id = "openai-chat-completions"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        authenticator: RequestAuthenticator | None = None,
        request_policy: ProviderRequestPolicy | None = None,
        provider_id: str | None = None,
        api_path: str = "/chat/completions",
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
            {"authorization": f"Bearer {api_key}"} if api_key else {}
        )
        self.request_policy = request_policy or ProviderRequestPolicy()
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
            payload = self._chat_completion_payload(
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
                events = self._events_from_streaming_chunks(
                    chunks, model=model, supports_tools=model.supports_tools
                )
                async with aclosing(events):
                    async for event in events:
                        yield event
            return

        response = await self._post_chat_completion(
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

    async def _post_chat_completion(
        self,
        *,
        headers: Mapping[str, str],
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
            url=f"{self.base_url}{self.api_path}",
            headers=headers,
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
        converted_messages = messages_to_chat_completions(
            messages,
            system_prompt=system_prompt,
            supports_image_input=model.supports_image_input,
            supports_audio_input=model.supports_audio_input,
            supports_video_input=model.supports_video_input,
            provider_id=self.provider_id,
            model_id=model.model_id,
            include_continuation=model.reasoning.continuation == "text",
        )
        payload: dict[str, Any] = dict(self.request_options)
        if "response_format" in payload and not model.supports_structured_output:
            raise ValueError("response_format requires an endpoint with structured output support")
        payload["model"] = model.model_id
        payload["messages"] = converted_messages
        if model.temperature is not None:
            payload["temperature"] = model.temperature
        else:
            payload.pop("temperature", None)
        if self.request_policy.supports_output_limit:
            payload["max_tokens"] = model.max_output_tokens
        else:
            payload.pop("max_tokens", None)
        self.reasoning_encoder.apply(payload, model)
        if tools and model.supports_tools:
            payload["tools"] = [_tool_to_chat_completions(tool) for tool in tools]
        else:
            payload.pop("tools", None)
        payload.update(extra_payload)
        return payload

    def _headers(self) -> dict[str, str]:
        return dict(self.default_headers)

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
        has_reasoning = bool(reasoning)
        if reasoning:
            yield ProviderStreamEvent(type="reasoning_delta", reasoning_content=reasoning)

        content = _content_to_text(message.get("content"))
        if content:
            yield ProviderStreamEvent(type="text_delta", text=content)
        refusal = _content_to_text(message.get("refusal"))
        if refusal:
            yield ProviderStreamEvent(type="text_delta", text=refusal)

        tool_calls = _tool_call_deltas(message)
        if tool_calls and not supports_tools:
            yield ProviderStreamEvent(
                type="error",
                error="provider returned tool calls for an endpoint without tool support",
            )
            return
        for tool_call in tool_calls:
            yield ProviderStreamEvent(type="tool_call_delta", tool_call_delta=tool_call)
        continuation = reasoning_fields_from_response(message)
        if continuation and model.reasoning.continuation == "text":
            yield ProviderStreamEvent(
                type="continuation",
                continuation=ProviderContinuationArtifact(
                    provider_id=self.provider_id,
                    protocol="openai-chat-completions",
                    model_id=model.model_id,
                    blocks=(continuation,),
                ),
            )
        yield ProviderStreamEvent(
            type="done",
            usage=usage,
            completion=_chat_completions_completion(
                choice.get("finish_reason"), has_reasoning=has_reasoning, refused=bool(refusal)
            ),
        )

    async def _events_from_streaming_chunks(
        self,
        chunks: AsyncIterator[Mapping[str, Any]],
        *,
        model: ModelMetadata,
        supports_tools: bool,
    ) -> AsyncGenerator[ProviderStreamEvent]:
        usage: Usage | None = None
        saw_reasoning = False
        saw_refusal = False
        continuation_reasoning: dict[str, list[Any]] = {}
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
            if model.reasoning.continuation == "text":
                for field, value in reasoning_fields_from_response(delta).items():
                    continuation_reasoning.setdefault(field, []).append(value)
            if reasoning:
                saw_reasoning = True
                yield ProviderStreamEvent(type="reasoning_delta", reasoning_content=reasoning)

            content = _content_to_text(delta.get("content"))
            if content:
                yield ProviderStreamEvent(type="text_delta", text=content)
            refusal = _content_to_text(delta.get("refusal"))
            if refusal:
                saw_refusal = True
                yield ProviderStreamEvent(type="text_delta", text=refusal)

            for tool_call_delta in _tool_call_deltas(delta):
                if not supports_tools:
                    yield ProviderStreamEvent(
                        type="error",
                        error="provider returned tool calls for an endpoint without tool support",
                    )
                    return
                yield ProviderStreamEvent(
                    type="tool_call_delta",
                    tool_call_delta=tool_call_delta,
                )

        if provider_finish_reason is None:
            yield ProviderStreamEvent(
                type="error",
                error="provider stream ended without finish_reason",
            )
            return

        if continuation_reasoning:
            continuation: dict[str, Any] = {}
            for field, fragments in continuation_reasoning.items():
                if all(isinstance(fragment, str) for fragment in fragments):
                    continuation[field] = "".join(fragments)
                elif all(isinstance(fragment, list) for fragment in fragments):
                    continuation[field] = [block for fragment in fragments for block in fragment]
                else:
                    raise ValueError("inconsistent Chat Completions reasoning field shape")
            yield ProviderStreamEvent(
                type="continuation",
                continuation=ProviderContinuationArtifact(
                    provider_id=self.provider_id,
                    protocol="openai-chat-completions",
                    model_id=model.model_id,
                    blocks=(continuation,),
                ),
            )
        yield ProviderStreamEvent(
            type="done",
            usage=usage,
            completion=_chat_completions_completion(
                provider_finish_reason, has_reasoning=saw_reasoning, refused=saw_refusal
            ),
        )


def _chat_completions_completion(
    value: object, *, has_reasoning: bool = False, refused: bool = False
) -> ProviderCompletion:
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
        finish_reason="content_filter" if refused else finish_reason,
        provider_finish_reason=provider_reason,
        has_reasoning=has_reasoning,
    )
