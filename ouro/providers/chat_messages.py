"""HTTP request lifecycle shared by Chat Completions and Messages adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Mapping
from contextlib import aclosing
from typing import Any

from ouro.auth.request_auth import RequestAuthenticator
from ouro.core.messages import ModelMessage
from ouro.core.provider import (
    ModelMetadata,
    ProviderCheckResult,
    ProviderRetryPolicy,
    ProviderStreamEvent,
)
from ouro.core.request_context import ProviderRequestContext
from ouro.providers.authentication import (
    ProviderRequestPolicy,
    request_authentication,
    safe_response_event,
)
from ouro.providers.connection_check import check_connection_events
from ouro.providers.headers import ProviderHeaderResolver
from ouro.providers.http_transport import ProviderHTTPTransport
from ouro.providers.request_context import model_request_context
from ouro.providers.retrying import stream_with_retries


class ChatMessageProvider(ABC):
    provider_id: str
    base_url: str
    api_path: str
    transport: ProviderHTTPTransport
    authenticator: RequestAuthenticator
    header_resolver: ProviderHeaderResolver
    default_headers: dict[str, str]
    request_policy: ProviderRequestPolicy
    retry_policy: ProviderRetryPolicy
    redact_errors: bool

    def _headers(self) -> Mapping[str, str]:
        return self.default_headers

    @abstractmethod
    def _stream_response(
        self,
        *,
        headers: Mapping[str, str],
        model: ModelMetadata,
        system_prompt: str,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]] | None,
    ) -> AsyncGenerator[ProviderStreamEvent]: ...

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

        async def attempt() -> AsyncGenerator[ProviderStreamEvent]:
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

        async with aclosing(stream_with_retries(attempt, self.retry_policy)) as attempts:
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
