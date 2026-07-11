from __future__ import annotations

from collections.abc import Callable

from ouro.config.provider_models import ProviderRuntimeOptions
from ouro.config.providers import ProviderConfigManager
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import ConfigError
from ouro.core.provider import AvailableModel, model_route_key
from ouro.providers.factory import build_runtime_adapter

AdapterBuilder = Callable[[ProviderRuntimeOptions], RuntimeAdapter]


class ConfiguredModelCatalog:
    """Expose enabled configured endpoints as durable session model routes."""

    def __init__(
        self,
        configs: ProviderConfigManager,
        default_adapter: RuntimeAdapter,
        *,
        adapter_builder: AdapterBuilder = build_runtime_adapter,
    ) -> None:
        self._configs = configs
        self._default_adapter = default_adapter
        self._adapter_builder = adapter_builder

    def list_models(self) -> list[AvailableModel]:
        models: list[AvailableModel] = []
        for provider in self._configs.snapshot().providers:
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
            model = self._default_adapter.model
            return AvailableModel(
                key=default_key,
                provider_id=model.provider_id,
                endpoint_id=model.endpoint_id or model.model_id,
                model_id=model.model_id,
                display_name=model.display_name,
            )

    def resolve(self, selector: str) -> AvailableModel:
        normalized = selector.strip()
        if not normalized:
            raise ConfigError("model selector is required")
        models = self.list_models()
        exact = next((model for model in models if model.key == normalized), None)
        if exact is not None:
            return exact

        aliases = [model for model in models if normalized in {model.endpoint_id, model.model_id}]
        runtime_default = self._runtime_default_if_matching(normalized)
        if runtime_default is not None and all(
            model.key != runtime_default.key for model in models
        ):
            aliases.append(runtime_default)
        if len(aliases) == 1:
            return aliases[0]
        if len(aliases) > 1:
            choices = ", ".join(model.key for model in aliases)
            raise ConfigError(f"ambiguous model selector {selector!r}; choose one of: {choices}")
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
        return self._adapter_builder(options)

    def _runtime_default_if_matching(self, selector: str) -> AvailableModel | None:
        model = self._default_adapter.model
        available = AvailableModel(
            key=model_route_key(model),
            provider_id=model.provider_id,
            endpoint_id=model.endpoint_id or model.model_id,
            model_id=model.model_id,
            display_name=model.display_name,
        )
        if selector in {available.key, available.endpoint_id, available.model_id}:
            return available
        return None
