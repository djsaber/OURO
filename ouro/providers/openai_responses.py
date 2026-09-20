"""Responses wire adapter, independent of subscription products and OAuth drivers."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncGenerator, Mapping
from contextlib import aclosing
from copy import deepcopy
from typing import Any

from ouro.auth.request_auth import RequestAuthenticator, StaticRequestAuthenticator
from ouro.config.provider_headers import ProviderHeaderValue
from ouro.config.provider_models import (
    validate_request_options,
    validate_responses_reasoning_profile,
)
from ouro.core.messages import ModelMessage
from ouro.core.provider import (
    ModelMetadata,
    ProviderCheckResult,
    ProviderRetryPolicy,
    ProviderStreamEvent,
)
from ouro.core.request_context import ProviderRequestContext
from ouro.providers.authentication import ProviderRequestPolicy, request_authentication
from ouro.providers.headers import ProviderHeaderResolver
from ouro.providers.http_transport import (
    HttpxProviderHTTPTransport,
    ProviderHTTPTransport,
    ProviderTransportError,
)
from ouro.providers.reasoning import ReasoningPayloadEncoder
from ouro.providers.request_context import model_request_context
from ouro.providers.responses_codec import responses_input, responses_tools
from ouro.providers.responses_errors import ResponsesError
from ouro.providers.responses_stream import responses_events
from ouro.providers.retrying import stream_with_retries


class OpenAIResponsesProvider:
    provider_id = "openai-responses"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        authenticator: RequestAuthenticator | None = None,
        request_policy: ProviderRequestPolicy | None = None,
        provider_id: str | None = None,
        endpoint: str = "/responses",
        timeout: float = 60,
        transport: ProviderHTTPTransport | None = None,
        default_headers: Mapping[str, str] | None = None,
        headers: Mapping[str, ProviderHeaderValue] | None = None,
        request_options: Mapping[str, Any] | None = None,
        reasoning_encoder: ReasoningPayloadEncoder | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
    ) -> None:
        if authenticator is not None and api_key is not None:
            raise ValueError("provide either an API Key or a request authenticator")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.provider_id = provider_id or self.provider_id
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint if endpoint.startswith("/") else "/" + endpoint
        self.timeout = timeout
        self.authenticator = authenticator or StaticRequestAuthenticator(
            {"authorization": "Bearer " + api_key} if api_key else {},
        )
        self.request_policy = request_policy or ProviderRequestPolicy()
        self.transport = transport or HttpxProviderHTTPTransport()
        self.default_headers = dict(default_headers or {})
        self.header_resolver = ProviderHeaderResolver(headers)
        self.redact_errors = bool(headers)
        self.request_options = deepcopy(dict(request_options or {}))
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
        headers = self.header_resolver.resolve(request_context, defaults=self.default_headers)
        attempts = stream_with_retries(
            lambda: self._stream_once(
                model=model,
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                headers=headers,
            ),
            self.retry_policy,
        )
        async with aclosing(attempts):
            async for event in attempts:
                # Visibility is separate from the retry boundary: hidden
                # reasoning still means the model has started responding.
                if event.type == "reasoning_delta" and model.reasoning.output not in {
                    "summary",
                    "text",
                }:
                    continue
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
        # Validate explicit controls before acquiring credentials or refreshing.
        payload = self._payload(model, tools)
        url = self.base_url + self.endpoint
        async with request_authentication(
            self.authenticator,
            url=url,
            headers=headers,
            policy=self.request_policy,
            redact_errors=self.redact_errors,
        ) as snapshot:
            try:
                payload.update(
                    responses_input(
                        messages,
                        system_prompt=system_prompt,
                        provider_id=self.provider_id,
                        model_id=model.model_id,
                        scope=snapshot.scope,
                    )
                )
                chunks = self._chunks(url, snapshot.headers, payload, model.supports_streaming)
                events = responses_events(
                    chunks,
                    provider_id=self.provider_id,
                    model_id=model.model_id,
                    scope=snapshot.scope,
                )
                deadline = asyncio.get_running_loop().time() + self.timeout
                async with aclosing(events):
                    while True:
                        # Do not leave a timeout armed while control belongs to
                        # the consumer of this generator.
                        try:
                            async with asyncio.timeout_at(deadline):
                                event = await anext(events)
                        except StopAsyncIteration:
                            break
                        if event.type == "tool_call_delta" and not model.supports_tools:
                            raise ResponsesError("unsupported_output_item")
                        yield event
            except ResponsesError as error:
                # Codec errors are already fixed local facts, including actionable
                # continuation mismatches; never replace them with raw service text.
                yield ProviderStreamEvent(type="error", error=str(error), error_code=error.code)
            except TimeoutError:
                raise ProviderTransportError("Model response timed out.", retryable=True) from None

    async def _chunks(
        self, url: str, headers: Mapping[str, str], payload: dict[str, Any], streaming: bool
    ) -> AsyncGenerator[dict[str, Any]]:
        if streaming:
            stream_json = getattr(self.transport, "stream_json", None)
            if not callable(stream_json):
                raise ValueError("Responses streaming requires an SSE transport")
            async with aclosing(
                stream_json(url=url, headers=headers, payload=payload, timeout=self.timeout)
            ) as chunks:
                async for chunk in chunks:
                    yield chunk
        else:
            response = await self.transport.post_json(
                url=url, headers=headers, payload=payload, timeout=self.timeout
            )
            status = response.get("status")
            kind = (
                {
                    "completed": "response.completed",
                    "incomplete": "response.incomplete",
                    "failed": "response.failed",
                }.get(status)
                if isinstance(status, str)
                else None
            )
            if kind is None:
                raise ResponsesError("invalid_terminal")
            yield {"type": kind, "response": response}

    def _payload(self, model: ModelMetadata, tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        validate_request_options(
            "openai-responses",
            self.request_options,
            supports_structured_output=model.supports_structured_output,
        )
        if model.supports_vision:
            raise ValueError("Responses image input is not implemented")
        if tools and not model.supports_tools:
            raise ValueError("endpoint does not support tools")
        profile = self.reasoning_encoder.profile
        if profile is not None:
            validate_responses_reasoning_profile(profile)
            if profile.owned_fields & self.request_options.keys():
                raise ValueError("request options conflict with reasoning-owned fields")
        payload = deepcopy(self.request_options)
        self.reasoning_encoder.apply(payload, model)
        payload.update(
            {
                "model": model.model_id,
                "stream": model.supports_streaming,
                "store": False,
                "include": ["reasoning.encrypted_content"],
            }
        )
        if self.request_policy.supports_output_limit:
            payload["max_output_tokens"] = model.max_output_tokens
        # Omit truncation: Responses defaults to disabled. Sending that redundant
        # field is rejected by some compatible services; overrides remain forbidden.
        if model.temperature is not None:
            payload["temperature"] = model.temperature
        if tools:
            payload["tools"] = responses_tools(tools)
        return payload

    async def check_connection(
        self, model: ModelMetadata, *, request_context: ProviderRequestContext | None = None
    ) -> ProviderCheckResult:
        request_context = model_request_context(model, request_context, purpose="connection_check")
        # Keep the endpoint's streaming contract and output budget; some models
        # consume reasoning tokens before any visible response.
        async with aclosing(
            self.stream(
                model=model,
                messages=[ModelMessage.text(role="user", text="Reply OK.")],
                request_context=request_context,
            )
        ) as events:
            async for event in events:
                if event.type == "error":
                    return ProviderCheckResult(
                        provider_id=self.provider_id,
                        model_id=model.model_id,
                        ok=False,
                        message=event.error or "Connection check failed.",
                        error_code=event.error_code,
                    )
        return ProviderCheckResult(
            provider_id=self.provider_id, model_id=model.model_id, ok=True, message="ok"
        )
