from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Protocol, Self

from pydantic import TypeAdapter

from ouro.core.cancellation import settle_owned_tasks
from ouro.core.messages import ModelMessage
from ouro.core.provider import ModelMetadata, ProviderCheckResult, ProviderStreamEvent
from ouro.core.redaction import SensitiveValueRedactor
from ouro.core.request_context import ProviderRequestContext
from ouro.plugins.invocation import plugin_invocation
from ouro.sdk.plugins import providers as sdk
from ouro.sdk.plugins.models import ModelReasoning

_MESSAGE = TypeAdapter(sdk.ProviderMessage)


@dataclass(frozen=True)
class ProviderRegistration:
    provider_id: str
    stream: sdk.ProviderStream
    check: sdk.ProviderCheckHandler
    plugin_id: str = ""
    owner: str = ""
    redactor: SensitiveValueRedactor = field(default_factory=SensitiveValueRedactor, repr=False)

    @property
    def canonical_id(self) -> str:
        return f"{self.plugin_id}/providers/{self.provider_id}"


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ProviderRegistration] = {}
        self._frozen = False

    def replace_owner(self, owner: str, registrations: tuple[ProviderRegistration, ...]) -> None:
        if self._frozen:
            raise RuntimeError("Provider registry is frozen")
        retained = {key: item for key, item in self._providers.items() if item.owner != owner}
        for item in registrations:
            if item.canonical_id in retained:
                raise ValueError(f"duplicate Provider: {item.canonical_id}")
            retained[item.canonical_id] = replace(item, owner=owner)
        self._providers = retained

    def freeze(self) -> None:
        self._frozen = True

    def get(self, provider_id: str) -> ProviderRegistration:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise ValueError(f"Provider is unavailable: {provider_id}") from exc


def adapt_provider(
    contribution: sdk.ProviderContribution, *, owner_id: str
) -> ProviderRegistration:
    async def stream(request: sdk.ProviderRequest, output: sdk.ProviderOutput) -> None:
        with plugin_invocation(
            owner_id,
            writable=True,
            session_id=request.context.get("session.id"),
            run_id=request.context.get("run.id"),
            inherit_run_ports=False,
        ):
            await contribution.stream(request, output)

    async def check(request: sdk.ProviderRequest) -> sdk.ProviderCheck:
        with plugin_invocation(
            owner_id,
            writable=True,
            session_id=request.context.get("session.id"),
            run_id=request.context.get("run.id"),
            inherit_run_ports=False,
        ):
            return await contribution.check(request)

    return ProviderRegistration(contribution.id, stream, check)


class ProviderBinding(Protocol):
    value: ProviderRegistration

    def __enter__(self) -> Self: ...

    def __exit__(self, *args: object) -> None: ...


ProviderBinder = Callable[[str], Awaitable[ProviderBinding]]
ConnectionResolver = Callable[[ProviderRequestContext | None], sdk.ProviderConnection]


@dataclass(frozen=True)
class _StreamEnd:
    error: BaseException | None


class PluginProvider:
    """Bind one generation per request; cached model routes own no plugin resources."""

    def __init__(
        self,
        provider_id: str,
        protocol: str,
        bind: ProviderBinder,
        connection: ConnectionResolver,
        *,
        sensitive_headers: frozenset[str] = frozenset(),
    ) -> None:
        self.provider_id, self.protocol = provider_id, protocol
        self._bind, self._connection = bind, connection
        self._sensitive_headers = sensitive_headers

    def _redactor(
        self, registration: ProviderRegistration, request: sdk.ProviderRequest
    ) -> SensitiveValueRedactor:
        if request.connection.api_key:
            registration.redactor.add(request.connection.api_key)
        for name in self._sensitive_headers:
            registration.redactor.add(request.connection.headers[name])
        return registration.redactor

    def _request(
        self,
        model: ModelMetadata,
        context: ProviderRequestContext | None,
        *,
        system_prompt: str = "",
        messages: list[ModelMessage] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> sdk.ProviderRequest:
        effective = model.reasoning_resolution.effective
        return sdk.ProviderRequest(
            connection=self._connection(context),
            model=sdk.ProviderModel(
                provider_id=self.provider_id,
                model_id=model.model_id,
                endpoint_id=model.endpoint_id,
                context_window=model.context_window,
                max_output_tokens=model.max_output_tokens,
                temperature=model.temperature,
                supports_tools=model.supports_tools,
                supports_streaming=model.supports_streaming,
                supports_structured_output=model.supports_structured_output,
                input_modalities=tuple(sorted(model.input_modalities)),
                reasoning=ModelReasoning(effective.mode, effective.effort),
            ),
            system_prompt=system_prompt,
            messages=tuple(_MESSAGE.validate_python(item.model_dump()) for item in messages or ()),
            tools=tuple(deepcopy(tools or [])),
            context=dict(context.values) if context is not None else {},
        )

    async def check_connection(
        self, model: ModelMetadata, *, request_context: ProviderRequestContext | None = None
    ) -> ProviderCheckResult:
        redactor = SensitiveValueRedactor()
        try:
            with await self._bind(self.protocol) as binding:
                request = self._request(model, request_context)
                redactor = self._redactor(binding.value, request)
                result = await binding.value.check(request)
                if not isinstance(result, sdk.ProviderCheck):
                    raise TypeError("Provider check must return ProviderCheck")
        except Exception as exc:
            return ProviderCheckResult(
                provider_id=self.provider_id,
                model_id=model.model_id,
                ok=False,
                message=redactor.redact(str(exc)),
                error_code="plugin_provider_check_failed",
            )
        return ProviderCheckResult(
            provider_id=self.provider_id,
            model_id=model.model_id,
            ok=result.ok,
            message=redactor.redact(result.message),
            error_code=redactor.redact(result.error_code)
            if result.error_code is not None
            else None,
        )

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages: list[ModelMessage],
        tools: list[dict[str, Any]] | None = None,
        request_context: ProviderRequestContext | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        with await self._bind(self.protocol) as binding:
            request = self._request(
                model, request_context, system_prompt=system_prompt, messages=messages, tools=tools
            )
            redactor = self._redactor(binding.value, request)
            queue: asyncio.Queue[ProviderStreamEvent | _StreamEnd] = asyncio.Queue(maxsize=1)
            consumer_closed = False
            output_closed = False

            async def output(value: sdk.ProviderEvent) -> None:
                if output_closed or consumer_closed:
                    raise RuntimeError("Provider output belongs to a finished request")
                if not isinstance(value, sdk.ProviderEvent):
                    raise TypeError("Provider output must be ProviderEvent")
                event = ProviderStreamEvent.model_validate(asdict(value))
                if event.error is not None:
                    event.error = redactor.redact(event.error)
                if event.error_code is not None:
                    event.error_code = redactor.redact(event.error_code)
                if event.retry is not None:
                    event.retry.reason = redactor.redact(event.retry.reason)
                await queue.put(event)

            async def produce() -> None:
                nonlocal output_closed
                error: BaseException | None = None
                try:
                    await binding.value.stream(request, output)
                except BaseException as exc:
                    error = exc
                finally:
                    output_closed = True
                if not consumer_closed:
                    await queue.put(_StreamEnd(error))

            producer = asyncio.create_task(produce(), name=f"provider:{self.protocol}")
            try:
                while True:
                    item = await queue.get()
                    if isinstance(item, _StreamEnd):
                        if isinstance(item.error, Exception):
                            raise RuntimeError(redactor.redact(str(item.error))) from None
                        if item.error is not None:
                            raise item.error
                        return
                    yield item
            finally:
                consumer_closed = True
                producer.cancel()
                cancelled = await settle_owned_tasks(producer)
                if not producer.cancelled():
                    producer.result()
                if cancelled:
                    raise asyncio.CancelledError
