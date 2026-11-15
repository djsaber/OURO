from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from ouro.application.configuration.facade import ConfigurationFacade
from ouro.application.context.prompts import PromptContext
from ouro.application.contracts.models import AvailableModel
from ouro.application.models.catalog import available_model, close_runtime_adapters, model_route_key
from ouro.config import (
    AgentDefinitionsConfigManager,
    CompactionConfig,
    CompactionConfigManager,
    PermissionConfigManager,
    PromptConfigManager,
    ProviderConfigManager,
    ProviderRuntimeOptions,
    ProviderTemplateStore,
    QueuedInputConfig,
    QueuedInputConfigManager,
    RunBudgetConfigManager,
    ToolExecutionConfig,
    ToolExecutionConfigManager,
)
from ouro.config.runtime import RuntimeSettingsResolver
from ouro.core.adapter import RuntimeAdapter
from ouro.core.context import compose_system_prompt
from ouro.core.errors import ConfigError
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.tools import PermissionMode
from ouro.mcp import MCPConfigStore
from ouro.skills import SkillManager, SkillMetadata, discover_skill_catalog

AdapterBuilder = Callable[[ProviderRuntimeOptions], RuntimeAdapter]


def build_local_configuration(root: str | Path | None = None) -> ConfigurationFacade:
    """Assemble local configuration use cases without starting an execution runtime."""
    return ConfigurationFacade(
        provider_templates=ProviderTemplateStore(root),
        providers=ProviderConfigManager(root),
        prompts=PromptConfigManager(root),
        permissions=PermissionConfigManager(root),
        tool_execution=ToolExecutionConfigManager(root),
        queued_inputs=QueuedInputConfigManager(root),
        run_budget=RunBudgetConfigManager(root),
        agents=AgentDefinitionsConfigManager(root),
        compaction=CompactionConfigManager(root),
        mcp=MCPConfigStore(root),
        skills=SkillManager(root),
    )


@dataclass(frozen=True)
class RuntimeConfigurationSnapshot:
    provider_options: ProviderRuntimeOptions
    system_prompt: str
    additional_system_prompt: str
    permission_overrides: dict[str, PermissionMode]
    plugin_permission_overrides: dict[str, PermissionMode]
    compaction: CompactionConfig
    tool_execution: ToolExecutionConfig
    run_budget: RunBudgetPolicy
    queued_inputs: QueuedInputConfig
    skills: tuple[SkillMetadata, ...]


class RuntimeConfigurationResolver:
    """Resolve startup configuration and explicitly refreshable request settings."""

    def __init__(self, root: str | Path | None = None) -> None:
        self._providers = ProviderConfigManager(root)
        self.root = self._providers.root
        self._prompts = PromptConfigManager(self.root)
        self.settings = RuntimeSettingsResolver(self.root)

    def snapshot(
        self,
        *,
        provider_options: ProviderRuntimeOptions | None = None,
        system_prompt: str | None = None,
    ) -> RuntimeConfigurationSnapshot:
        prompts = self._prompts.snapshot().config
        settings = self.settings.snapshot()
        permissions = settings.permissions
        return RuntimeConfigurationSnapshot(
            provider_options=self._providers.resolve_runtime_options(
                provider_options or ProviderRuntimeOptions()
            ),
            system_prompt=prompts.resolve_system_prompt(system_prompt),
            additional_system_prompt=prompts.additional_system_prompt,
            permission_overrides=permissions.tools,
            plugin_permission_overrides=permissions.plugins,
            compaction=settings.compaction,
            tool_execution=settings.tool_execution,
            run_budget=settings.run_budget,
            queued_inputs=settings.queued_inputs,
            skills=self.skills(),
        )

    def skills(self) -> tuple[SkillMetadata, ...]:
        """Discover the current directory entries and source policy on demand."""

        return tuple(discover_skill_catalog(home=self.root).skills)

    def prompt_context(self, *, system_prompt: str | None = None) -> PromptContext:
        prompts = self._prompts.snapshot().config
        replacement = next((entry for entry in prompts.replacements if entry.enabled), None)
        return PromptContext(
            system_prompt=compose_system_prompt(
                prompts.resolve_system_prompt(system_prompt), prompts.additional_system_prompt
            ),
            source=(
                "override"
                if system_prompt is not None
                else "replacement"
                if replacement
                else "default"
            ),
            revision=prompts.revision,
            replacement_id=(
                replacement.id if replacement is not None and system_prompt is None else None
            ),
            addition_ids=tuple(entry.id for entry in prompts.additions if entry.enabled),
        )

    def model_catalog(
        self,
        default_adapter: RuntimeAdapter,
        *,
        adapter_builder: AdapterBuilder,
        default_overrides: ProviderRuntimeOptions | None = None,
        default_options: ProviderRuntimeOptions | None = None,
    ) -> ConfiguredModelCatalog:
        return ConfiguredModelCatalog(
            self._providers,
            default_adapter,
            adapter_builder=adapter_builder,
            default_overrides=default_overrides,
            default_options=default_options,
        )

    def compaction_provider_options(
        self,
        config: CompactionConfig,
    ) -> ProviderRuntimeOptions | None:
        model = config.model
        if not model.has_configured_route:
            return None
        if model.use_default_model:
            provider_id, endpoint_id = self._providers.require_default_route().split("/", 1)
        else:
            assert model.provider_id is not None
            assert model.endpoint_id is not None
            provider_id, endpoint_id = model.provider_id, model.endpoint_id
        return self._providers.resolve_runtime_options(
            ProviderRuntimeOptions(
                provider_id=provider_id,
                endpoint_id=endpoint_id,
            )
        )

    def default_model_route(self) -> str:
        return self._providers.require_default_route()


class ConfiguredModelCatalog:
    """Expose enabled configured endpoints as durable session model routes."""

    def __init__(
        self,
        configs: ProviderConfigManager,
        default_adapter: RuntimeAdapter | None = None,
        *,
        adapter_builder: AdapterBuilder,
        default_overrides: ProviderRuntimeOptions | None = None,
        default_options: ProviderRuntimeOptions | None = None,
    ) -> None:
        self._configs = configs
        self._default_adapter = default_adapter
        self._adapter_builder = adapter_builder
        self._configured_adapters: dict[str, tuple[str, RuntimeAdapter]] = {}
        self._retired_adapters: list[RuntimeAdapter] = []
        self._adapter_lock = Lock()
        self._closed = False
        self._default_overrides = default_overrides or ProviderRuntimeOptions()
        self._default_is_configured = False
        self._default_route_pinned = False
        if default_adapter is None:
            return
        default = default_adapter.model
        provider = configs.get_provider(default.provider_id)
        self._default_is_configured = (
            provider is not None
            and provider.endpoint(default.endpoint_id or default.model_id) is not None
        )
        if default_options is not None:
            # A removal after the startup snapshot must not turn a configured
            # route into an ad-hoc adapter that bypasses subsequent validation.
            self._default_is_configured = (
                default_options.endpoint_id is not None
                and default_options.provider_id == default.provider_id
                and default_options.endpoint_id == (default.endpoint_id or default.model_id)
            )
        if self._default_is_configured:
            options = default_options or self._options_for(
                default.provider_id, default.endpoint_id or default.model_id
            )
            self._configured_adapters[model_route_key(default)] = (
                _options_fingerprint(options),
                default_adapter,
            )
        # A startup composition for a route outside the configured default
        # (CLI provider/endpoint flags) owns the process default; otherwise the
        # configured default route stays live so default-route changes apply to
        # the next request without rebuilding the runtime.
        self._default_route_pinned = (
            model_route_key(default_adapter.model) != self._configured_default_route()
        )

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
                        provider_display_name=provider.display_name,
                        endpoint_id=endpoint.endpoint_id,
                        model_id=endpoint.model_id,
                        display_name=endpoint.display_name,
                        temperature=endpoint.temperature,
                        context_window=endpoint.context_window,
                        max_output_tokens=endpoint.max_output_tokens,
                        reasoning=endpoint.reasoning.capability,
                    )
                )
        return sorted(models, key=lambda item: item.key)

    def default_model(self) -> AvailableModel:
        """Resolve the default route from current configuration.

        Follows the configured default provider/endpoint route so runtime
        default-route changes apply without a restart. A startup composition
        for a different route stays pinned, and unresolvable configuration
        falls back to the last known startup route.
        """

        if self._default_adapter is None:
            return self.configured_default_model()
        if not self._default_route_pinned:
            route = self._configured_default_route()
            if route is not None:
                try:
                    return self.resolve(route)
                except ConfigError:
                    pass
        return available_model(self._default_adapter.model)

    def configured_default_model(self) -> AvailableModel:
        """Resolve the user-selected system route without startup or Faux fallbacks."""

        return self.resolve(self._configs.require_default_route())

    def _configured_default_route(self) -> str | None:
        try:
            provider = self._configs.default_provider()
        except ConfigError:
            return None
        if provider.default_endpoint is None:
            return None
        return f"{provider.provider_id}/{provider.default_endpoint}"

    def resolve(self, selector: str) -> AvailableModel:
        normalized = selector.strip()
        if not normalized:
            raise ConfigError("model selector is required")
        models = self.list_models()
        exact = next((model for model in models if model.key == normalized), None)
        if exact is not None:
            return exact

        if self._default_adapter is not None:
            runtime_default = available_model(self._default_adapter.model)
            if normalized == runtime_default.key:
                return runtime_default
        raise ConfigError(f"model not found: {selector}")

    def adapter_for(self, selector: str) -> RuntimeAdapter:
        # Session request preparation can run on different worker threads. Publish
        # a single owned adapter per fingerprint so no losing build leaks a client.
        with self._adapter_lock:
            if self._closed:
                raise RuntimeError("model catalog is closed")
            return self._adapter_for(selector)

    def _adapter_for(self, selector: str) -> RuntimeAdapter:
        model = self.resolve(selector)
        if (
            self._default_adapter is not None
            and not self._default_is_configured
            and model.key == model_route_key(self._default_adapter.model)
        ):
            return self._default_adapter
        provider = self._configs.get_provider(model.provider_id)
        endpoint = (
            provider.endpoint(model.endpoint_id)
            if provider is not None and provider.enabled
            else None
        )
        if endpoint is None:
            raise ConfigError(f"model endpoint is unavailable: {model.key}")
        options = self._options_for(model.provider_id, model.endpoint_id)
        fingerprint = _options_fingerprint(options)
        cached = self._configured_adapters.get(model.key)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        adapter = self._adapter_builder(options)
        if cached is not None:
            self._retired_adapters.append(cached[1])
        self._configured_adapters[model.key] = (fingerprint, adapter)
        return adapter

    def _options_for(self, provider_id: str, endpoint_id: str) -> ProviderRuntimeOptions:
        overrides = (
            self._default_overrides
            if self._default_adapter is not None
            and f"{provider_id}/{endpoint_id}" == model_route_key(self._default_adapter.model)
            else ProviderRuntimeOptions()
        )
        return self._configs.resolve_runtime_options(
            overrides.model_copy(update={"provider_id": provider_id, "endpoint_id": endpoint_id})
        )

    async def close(self) -> None:
        with self._adapter_lock:
            if self._closed:
                return
            self._closed = True
            adapters = (
                *((self._default_adapter,) if self._default_adapter is not None else ()),
                *(adapter for _, adapter in self._configured_adapters.values()),
                *self._retired_adapters,
            )
            self._configured_adapters.clear()
            self._retired_adapters.clear()
        await close_runtime_adapters(adapters)


def _options_fingerprint(options: ProviderRuntimeOptions) -> str:
    # repr intentionally hides inline credentials. Hash canonical data instead;
    # neither the serialized material nor the fingerprint is exposed to Hosts.
    payload = json.dumps(options.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
