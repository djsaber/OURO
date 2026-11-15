"""Model availability queries and reasoning selection."""

from __future__ import annotations

import inspect
from typing import Protocol

from ouro.application.contracts.models import AvailableModel
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import ConfigError
from ouro.core.provider import ModelMetadata


class RuntimeAdapterCatalog(Protocol):
    """Resolve durable model route keys into request-local runtime adapters."""

    def list_models(self) -> list[AvailableModel]: ...

    def default_model(self) -> AvailableModel: ...

    def configured_default_model(self) -> AvailableModel: ...

    def resolve(self, selector: str) -> AvailableModel: ...

    def adapter_for(self, selector: str) -> RuntimeAdapter: ...

    async def close(self) -> None: ...


class FixedRuntimeAdapterCatalog:
    """Expose one injected adapter through the same route contract as configured models."""

    def __init__(self, adapter: RuntimeAdapter) -> None:
        self._adapter = adapter
        self._model = available_model(adapter.model)

    def list_models(self) -> list[AvailableModel]:
        return [self._model]

    def default_model(self) -> AvailableModel:
        return self._model

    def configured_default_model(self) -> AvailableModel:
        raise ConfigError("system default endpoint is not configured")

    def resolve(self, selector: str) -> AvailableModel:
        normalized = selector.strip()
        if normalized != self._model.key:
            raise ConfigError(f"model not found: {selector}")
        return self._model

    def adapter_for(self, selector: str) -> RuntimeAdapter:
        self.resolve(selector)
        return self._adapter

    async def close(self) -> None:
        await close_runtime_adapters((self._adapter,))


def model_route_key(model: ModelMetadata) -> str:
    """Return the durable provider/endpoint key stored on a session."""

    endpoint_id = model.endpoint_id or model.model_id
    return f"{model.provider_id}/{endpoint_id}"


def available_model(model: ModelMetadata) -> AvailableModel:
    return AvailableModel(
        key=model_route_key(model),
        provider_id=model.provider_id,
        endpoint_id=model.endpoint_id or model.model_id,
        model_id=model.model_id,
        display_name=model.display_name,
        temperature=model.temperature,
        context_window=model.context_window,
        max_output_tokens=model.max_output_tokens,
        reasoning=model.reasoning,
    )


async def close_runtime_adapters(adapters: tuple[RuntimeAdapter, ...]) -> None:
    seen: set[int] = set()
    errors: list[Exception] = []
    for adapter in adapters:
        provider = adapter.provider
        if id(provider) in seen:
            continue
        seen.add(id(provider))
        close = getattr(provider, "close", None)
        if close is None:
            continue
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - close every owned provider before reporting.
            errors.append(exc)
    if errors:
        raise ExceptionGroup("runtime provider shutdown failed", errors)
