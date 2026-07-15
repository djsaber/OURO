from __future__ import annotations

import hashlib
import inspect
from collections.abc import Callable
from typing import Protocol

from ouro.config.provider_models import ProviderRuntimeOptions
from ouro.config.providers import ProviderConfigManager
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError
from ouro.core.provider import ModelMetadata

AdapterBuilder = Callable[[ProviderRuntimeOptions], RuntimeAdapter]


class AvailableModel(OuroModel):
    """Stable session-selectable route to one configured model endpoint."""

    key: str
    provider_id: str
    endpoint_id: str
    model_id: str
    display_name: str | None = None


class RuntimeAdapterCatalog(Protocol):
    """Resolve durable model route keys into request-local runtime adapters."""

    def list_models(self) -> list[AvailableModel]: ...

    def default_model(self) -> AvailableModel: ...

    def resolve(self, selector: str) -> AvailableModel: ...

    def adapter_for(self, selector: str) -> RuntimeAdapter: ...


class FixedRuntimeAdapterCatalog:
    """Expose one injected adapter through the same route contract as configured models."""

    def __init__(self, adapter: RuntimeAdapter) -> None:
        self._adapter = adapter
        self._model = _available_model(adapter.model)

    def list_models(self) -> list[AvailableModel]:
        return [self._model]

    def default_model(self) -> AvailableModel:
        return self._model

    def resolve(self, selector: str) -> AvailableModel:
        normalized = selector.strip()
        if normalized != self._model.key:
            raise ConfigError(f"model not found: {selector}")
        return self._model

    def adapter_for(self, selector: str) -> RuntimeAdapter:
        self.resolve(selector)
        return self._adapter

    async def close(self) -> None:
        await _close_adapters((self._adapter,))


class ConfiguredModelCatalog:
    """Expose enabled configured endpoints as durable session model routes."""

    def __init__(
        self,
        configs: ProviderConfigManager,
        default_adapter: RuntimeAdapter,
        *,
        adapter_builder: AdapterBuilder,
    ) -> None:
        self._configs = configs
        self._default_adapter = default_adapter
        self._adapter_builder = adapter_builder
        self._configured_adapters: dict[str, tuple[str, RuntimeAdapter]] = {}
        self._retired_adapters: list[RuntimeAdapter] = []

    def list_models(self) -> list[AvailableModel]:
        models: list[AvailableModel] = []
        for provider in self._configs.snapshot(include_builtin=False).providers:
            if not provider.enabled:
                continue
            for endpoint in provider.endpoints:
                models.append(
                    AvailableModel(
                        key=f"{provider.provider_id}/{endpoint.endpoint_id}",
                        provider_id=provider.provider_id,
                        endpoint_id=endpoint.endpoint_id,
                        model_id=endpoint.model_id,
                        display_name=str(endpoint.metadata.get("display_name") or "") or None,
                    )
                )
        return sorted(models, key=lambda item: item.key)

    def default_model(self) -> AvailableModel:
        default_key = model_route_key(self._default_adapter.model)
        try:
            return self.resolve(default_key)
        except ConfigError:
            return _available_model(self._default_adapter.model)

    def resolve(self, selector: str) -> AvailableModel:
        normalized = selector.strip()
        if not normalized:
            raise ConfigError("model selector is required")
        models = self.list_models()
        exact = next((model for model in models if model.key == normalized), None)
        if exact is not None:
            return exact

        runtime_default = _available_model(self._default_adapter.model)
        if normalized == runtime_default.key:
            return runtime_default
        raise ConfigError(f"model not found: {selector}")

    def adapter_for(self, selector: str) -> RuntimeAdapter:
        model = self.resolve(selector)
        if model.key == model_route_key(self._default_adapter.model):
            return self._default_adapter
        provider = self._configs.get_provider(model.provider_id)
        endpoint = (
            provider.endpoint(model.endpoint_id)
            if provider is not None and provider.enabled
            else None
        )
        if endpoint is None:
            raise ConfigError(f"model endpoint is unavailable: {model.key}")
        options = self._configs.resolve_runtime_options(
            ProviderRuntimeOptions(
                provider_id=model.provider_id,
                endpoint_id=model.endpoint_id,
            )
        )
        fingerprint = hashlib.sha256(repr(options).encode("utf-8")).hexdigest()
        cached = self._configured_adapters.get(model.key)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        adapter = self._adapter_builder(options)
        if cached is not None:
            self._retired_adapters.append(cached[1])
        self._configured_adapters[model.key] = (fingerprint, adapter)
        return adapter

    async def close(self) -> None:
        await _close_adapters(
            (
                self._default_adapter,
                *(adapter for _, adapter in self._configured_adapters.values()),
                *self._retired_adapters,
            )
        )
        self._configured_adapters.clear()
        self._retired_adapters.clear()


def model_route_key(model: ModelMetadata) -> str:
    """Return the durable provider/endpoint key stored on a session."""

    endpoint_id = model.endpoint_id or model.model_id
    return f"{model.provider_id}/{endpoint_id}"


def _available_model(model: ModelMetadata) -> AvailableModel:
    return AvailableModel(
        key=model_route_key(model),
        provider_id=model.provider_id,
        endpoint_id=model.endpoint_id or model.model_id,
        model_id=model.model_id,
        display_name=model.display_name,
    )


async def _close_adapters(adapters: tuple[RuntimeAdapter, ...]) -> None:
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
