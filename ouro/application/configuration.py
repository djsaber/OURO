from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ouro.application.configuration_models import (
    CompactionConfigView,
    ConfigurationChange,
    ConfigurationOverview,
    LoadedSkillView,
    PermissionConfigView,
    ProviderCatalogView,
    ProviderEndpointCatalogView,
    ProviderEndpointView,
    ProviderTemplateCatalogView,
    ProviderTemplateView,
    ProviderView,
    QueuedInputConfigView,
    RunBudgetConfigView,
    SkillCatalogView,
    SkillManagementView,
    SkillPolicyView,
    SkillRootView,
    SkillsPromptView,
    SkillValidationView,
    SystemPromptConfigView,
    ToolExecutionConfigView,
)
from ouro.application.configuration_views import (
    compaction_view,
    configuration_change,
    endpoint_catalog_view,
    endpoint_view,
    loaded_skill_view,
    managed_skill_view,
    provider_template_view,
    provider_view,
    skill_diagnostic_view,
    skill_management_view,
    skill_view,
)
from ouro.config import (
    BUILTIN_FAUX_PROVIDER_ID,
    INTEGRATED_PROVIDER_TYPES,
    CompactionConfigManager,
    EndpointReasoningConfig,
    PermissionConfigManager,
    PromptConfigManager,
    ProviderConfig,
    ProviderConfigManager,
    ProviderType,
    QueuedInputConfigManager,
    QueuedInputConsumptionMode,
    RunBudgetConfigManager,
    ToolExecutionConfigManager,
    get_provider_template,
)
from ouro.config import (
    provider_templates as available_provider_templates,
)
from ouro.config.skills import SkillInstallScope
from ouro.core.errors import ConfigError
from ouro.core.provider import ProviderRetryPolicy
from ouro.core.reasoning import ReasoningRequest
from ouro.core.tool_batches import ToolExecutionMode
from ouro.core.tools import PermissionMode
from ouro.prompts import render_agent_skills_prompt
from ouro.skills import (
    SkillLoader,
    SkillManager,
    default_skill_roots,
    discover_skill_catalog,
)


class ConfigurationFacade:
    """Typed configuration use cases shared by CLI and future hosts."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.providers = ProviderConfigManager(root)
        self.prompts = PromptConfigManager(root)
        self.permissions = PermissionConfigManager(root)
        self.tool_execution = ToolExecutionConfigManager(root)
        self.queued_inputs = QueuedInputConfigManager(root)
        self.run_budget = RunBudgetConfigManager(root)
        self.compaction = CompactionConfigManager(root)
        self.skills = SkillManager(root)
        self._skill_policy = self.skills.config
        self.root = self.providers.root
        self.config_path = self.providers.config_path

    def overview(self) -> ConfigurationOverview:
        return ConfigurationOverview(
            config_path=str(self.config_path),
            providers=self.provider_catalog(),
            prompt=self.system_prompt(),
            permissions=self.permission_config(),
            tool_execution=self.tool_execution_config(),
            queued_inputs=self.queued_input_config(),
            run_budget=self.run_budget_config(),
            compaction=self.compaction_config(),
            skills=self.skill_policy(),
        )

    def provider_catalog(
        self,
        *,
        change: ConfigurationChange | None = None,
    ) -> ProviderCatalogView:
        snapshot = self.providers.snapshot(include_builtin=False)
        default_provider_id = (
            None
            if snapshot.default_provider_id == BUILTIN_FAUX_PROVIDER_ID
            else snapshot.default_provider_id
        )
        return ProviderCatalogView(
            config_path=str(snapshot.config_path),
            default_provider_id=default_provider_id,
            providers=[
                provider_view(
                    provider,
                    is_default=provider.provider_id == snapshot.default_provider_id,
                )
                for provider in snapshot.providers
            ],
            change=change,
        )

    def provider(self, provider_id: str | None = None) -> ProviderView:
        snapshot = self.providers.snapshot(include_builtin=False)
        target_id = provider_id or (
            None
            if snapshot.default_provider_id == BUILTIN_FAUX_PROVIDER_ID
            else snapshot.default_provider_id
        )
        if target_id is None:
            raise ConfigError("default provider is not configured")
        provider = self.providers.get_provider(target_id, include_builtin=False)
        if provider is None:
            raise ConfigError(f"provider not found: {target_id}")
        return provider_view(
            provider,
            is_default=provider.provider_id == snapshot.default_provider_id,
        )

    def provider_template_catalog(self) -> ProviderTemplateCatalogView:
        return ProviderTemplateCatalogView(
            templates=[
                provider_template_view(template) for template in available_provider_templates()
            ]
        )

    def provider_template(self, template_id: str) -> ProviderTemplateView:
        return provider_template_view(get_provider_template(template_id))

    def add_provider(
        self,
        provider_id: str,
        *,
        provider_type: ProviderType,
        endpoint_id: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        retry_max_attempts: int | None = None,
        retry_initial_backoff_seconds: float | None = None,
        retry_max_backoff_seconds: float | None = None,
        retry_backoff_multiplier: float | None = None,
        enabled: bool = True,
        make_default: bool = False,
    ) -> ProviderCatalogView:
        retry_policy = self._resolve_provider_retry_policy(
            provider_id,
            preserve_current=False,
            retry_policy=retry_policy,
            max_attempts=retry_max_attempts,
            initial_backoff_seconds=retry_initial_backoff_seconds,
            max_backoff_seconds=retry_max_backoff_seconds,
            backoff_multiplier=retry_backoff_multiplier,
        )
        if provider_type in INTEGRATED_PROVIDER_TYPES:
            self.providers.create_from_template(
                get_provider_template(provider_type),
                provider_id=provider_id,
                endpoint_id=endpoint_id,
                model_id=model,
                base_url=base_url,
                api_path=api_path,
                api_key=api_key,
                api_key_env=api_key_env,
                timeout_seconds=timeout,
                retry_policy=retry_policy,
                enabled=enabled,
                make_default=make_default,
            )
        else:
            self._reject_template_options(
                provider_type,
                endpoint_id=endpoint_id,
                model=model,
                make_default=make_default,
            )
            self.providers.create_provider(
                provider_id,
                provider_type=provider_type,
                base_url=base_url,
                api_path=api_path,
                api_key=api_key,
                api_key_env=api_key_env,
                timeout_seconds=timeout,
                retry_policy=retry_policy,
                enabled=enabled,
            )
        return self.provider_catalog(
            change=configuration_change("providers", "created", "next_request")
        )

    def update_provider(
        self,
        provider_id: str,
        *,
        endpoint_id: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        retry_max_attempts: int | None = None,
        retry_initial_backoff_seconds: float | None = None,
        retry_max_backoff_seconds: float | None = None,
        retry_backoff_multiplier: float | None = None,
        enabled: bool | None = None,
        make_default: bool = False,
    ) -> ProviderCatalogView:
        retry_policy = self._resolve_provider_retry_policy(
            provider_id,
            preserve_current=True,
            retry_policy=retry_policy,
            max_attempts=retry_max_attempts,
            initial_backoff_seconds=retry_initial_backoff_seconds,
            max_backoff_seconds=retry_max_backoff_seconds,
            backoff_multiplier=retry_backoff_multiplier,
        )
        current = self._require_provider(provider_id)
        if current.provider_type in INTEGRATED_PROVIDER_TYPES:
            self.providers.update_from_template(
                get_provider_template(current.provider_type),
                provider_id=provider_id,
                endpoint_id=endpoint_id,
                model_id=model,
                base_url=base_url,
                api_path=api_path,
                api_key=api_key,
                api_key_env=api_key_env,
                timeout_seconds=timeout,
                retry_policy=retry_policy,
                enabled=enabled,
                make_default=make_default,
            )
        else:
            self._reject_template_options(
                current.provider_type,
                endpoint_id=endpoint_id,
                model=model,
                make_default=False,
            )
            self.providers.update_provider(
                provider_id,
                base_url=base_url,
                api_path=api_path,
                api_key=api_key,
                api_key_env=api_key_env,
                timeout_seconds=timeout,
                retry_policy=retry_policy,
                enabled=enabled,
                make_default=make_default,
            )
        return self.provider_catalog(
            change=configuration_change("providers", "updated", "next_request")
        )

    def replace_provider(
        self,
        provider_id: str,
        *,
        provider_type: ProviderType,
        endpoint_id: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        retry_max_attempts: int | None = None,
        retry_initial_backoff_seconds: float | None = None,
        retry_max_backoff_seconds: float | None = None,
        retry_backoff_multiplier: float | None = None,
        enabled: bool = True,
        make_default: bool = False,
    ) -> ProviderCatalogView:
        retry_policy = self._resolve_provider_retry_policy(
            provider_id,
            preserve_current=False,
            retry_policy=retry_policy,
            max_attempts=retry_max_attempts,
            initial_backoff_seconds=retry_initial_backoff_seconds,
            max_backoff_seconds=retry_max_backoff_seconds,
            backoff_multiplier=retry_backoff_multiplier,
        )
        if provider_type in INTEGRATED_PROVIDER_TYPES:
            self.providers.replace_from_template(
                get_provider_template(provider_type),
                provider_id=provider_id,
                endpoint_id=endpoint_id,
                model_id=model,
                base_url=base_url,
                api_path=api_path,
                api_key=api_key,
                api_key_env=api_key_env,
                timeout_seconds=timeout,
                retry_policy=retry_policy,
                enabled=enabled,
                make_default=make_default,
            )
        else:
            self._reject_template_options(
                provider_type,
                endpoint_id=endpoint_id,
                model=model,
                make_default=make_default,
            )
            self.providers.replace_provider(
                provider_id,
                provider_type=provider_type,
                base_url=base_url,
                api_path=api_path,
                api_key=api_key,
                api_key_env=api_key_env,
                timeout_seconds=timeout,
                retry_policy=retry_policy,
                enabled=enabled,
            )
        return self.provider_catalog(
            change=configuration_change("providers", "replaced", "next_request")
        )

    def _resolve_provider_retry_policy(
        self,
        provider_id: str,
        *,
        preserve_current: bool,
        retry_policy: ProviderRetryPolicy | None,
        max_attempts: int | None,
        initial_backoff_seconds: float | None,
        max_backoff_seconds: float | None,
        backoff_multiplier: float | None,
    ) -> ProviderRetryPolicy | None:
        updates = {
            key: value
            for key, value in {
                "max_attempts": max_attempts,
                "initial_backoff_seconds": initial_backoff_seconds,
                "max_backoff_seconds": max_backoff_seconds,
                "backoff_multiplier": backoff_multiplier,
            }.items()
            if value is not None
        }
        if retry_policy is not None and updates:
            raise ConfigError("retry_policy cannot be combined with individual retry options")
        if not updates:
            return retry_policy
        current = self.providers.get_provider(provider_id) if preserve_current else None
        if preserve_current and current is None:
            raise ConfigError(f"provider not found: {provider_id}")
        base_retry = current.retry_policy if current is not None else ProviderRetryPolicy()
        try:
            return ProviderRetryPolicy.model_validate(
                base_retry.model_copy(update=updates).model_dump()
            )
        except ValueError as exc:
            raise ConfigError(f"invalid provider retry policy: {exc}") from exc

    @staticmethod
    def _reject_template_options(
        provider_type: ProviderType,
        *,
        endpoint_id: str | None,
        model: str | None,
        make_default: bool,
    ) -> None:
        if endpoint_id is not None or model is not None:
            raise ConfigError(
                "--endpoint-id and --model require an integrated provider type; "
                f"{provider_type} uses explicit endpoint commands"
            )
        if make_default:
            raise ConfigError(
                "--default requires a provider with a default endpoint; "
                "add an endpoint and then use provider use"
            )

    def use_provider(self, provider_id: str) -> ProviderCatalogView:
        self._require_provider(provider_id)
        self.providers.set_default_provider(provider_id)
        return self.provider_catalog(
            change=configuration_change("providers", "default_changed", "next_request")
        )

    def remove_provider(self, provider_id: str) -> ProviderCatalogView:
        self.providers.remove_provider(provider_id)
        return self.provider_catalog(
            change=configuration_change("providers", "removed", "next_request")
        )

    def endpoint_catalog(self, provider_id: str) -> ProviderEndpointCatalogView:
        provider = self._require_provider(provider_id)
        return endpoint_catalog_view(provider)

    def endpoint(self, provider_id: str, endpoint_id: str | None = None) -> ProviderEndpointView:
        provider = self._require_provider(provider_id)
        endpoint = provider.endpoint(endpoint_id)
        if endpoint is None:
            target = endpoint_id or provider.default_endpoint or "<default>"
            raise ConfigError(f"endpoint not found: {provider_id}/{target}")
        return endpoint_view(endpoint, default_endpoint=provider.default_endpoint)

    def save_endpoint(
        self,
        provider_id: str,
        endpoint_id: str,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        temperature: float | None = None,
        context_window: int | None = None,
        max_output_tokens: int | None = None,
        supports_tools: bool | None = None,
        supports_vision: bool | None = None,
        supports_streaming: bool | None = None,
        supports_structured_output: bool | None = None,
        reasoning: EndpointReasoningConfig | None = None,
        request_options: Mapping[str, Any] | None = None,
        make_default: bool = False,
    ) -> ProviderEndpointCatalogView:
        self.providers.upsert_endpoint(
            provider_id,
            endpoint_id,
            model_id=model,
            base_url=base_url,
            api_path=api_path,
            temperature=temperature,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            supports_tools=supports_tools,
            supports_vision=supports_vision,
            supports_streaming=supports_streaming,
            supports_structured_output=supports_structured_output,
            reasoning=reasoning,
            request_options=request_options,
            make_default=make_default,
        )
        return endpoint_catalog_view(
            self._require_provider(provider_id),
            change=configuration_change("providers", "endpoint_saved", "next_request"),
        )

    def use_endpoint(self, provider_id: str, endpoint_id: str) -> ProviderEndpointCatalogView:
        self.providers.set_default_endpoint(provider_id, endpoint_id)
        return endpoint_catalog_view(
            self._require_provider(provider_id),
            change=configuration_change("providers", "default_endpoint_changed", "next_request"),
        )

    def remove_endpoint(
        self,
        provider_id: str,
        endpoint_id: str,
    ) -> ProviderEndpointCatalogView:
        self.providers.remove_endpoint(provider_id, endpoint_id)
        return endpoint_catalog_view(
            self._require_provider(provider_id),
            change=configuration_change("providers", "endpoint_removed", "next_request"),
        )

    def system_prompt(
        self,
        *,
        change: ConfigurationChange | None = None,
    ) -> SystemPromptConfigView:
        snapshot = self.prompts.snapshot()
        public = self.prompts.resolve_system_prompt()
        return SystemPromptConfigView(
            config_path=str(snapshot.config_path),
            configured=snapshot.has_system_prompt,
            system_prompt=snapshot.system_prompt,
            effective_system_prompt=public,
            change=change,
        )

    def set_system_prompt(self, prompt: str) -> SystemPromptConfigView:
        self.prompts.set_system_prompt(prompt)
        return self.system_prompt(change=configuration_change("prompts", "saved", "next_runtime"))

    def clear_system_prompt(self) -> SystemPromptConfigView:
        self.prompts.clear_system_prompt()
        return self.system_prompt(change=configuration_change("prompts", "cleared", "next_runtime"))

    def permission_config(
        self,
        *,
        change: ConfigurationChange | None = None,
    ) -> PermissionConfigView:
        snapshot = self.permissions.snapshot()
        return PermissionConfigView(
            config_path=str(snapshot.config_path),
            tools=snapshot.tools,
            change=change,
        )

    def set_tool_permission(self, tool_name: str, mode: PermissionMode) -> PermissionConfigView:
        self.permissions.set_tool_mode(tool_name, mode)
        return self.permission_config(
            change=configuration_change("permissions", "tool_mode_saved", "next_runtime")
        )

    def clear_tool_permission(self, tool_name: str) -> PermissionConfigView:
        self.permissions.clear_tool_mode(tool_name)
        return self.permission_config(
            change=configuration_change("permissions", "tool_mode_cleared", "next_runtime")
        )

    def tool_execution_config(
        self,
        *,
        change: ConfigurationChange | None = None,
    ) -> ToolExecutionConfigView:
        config = self.tool_execution.snapshot()
        return ToolExecutionConfigView(
            config_path=str(self.tool_execution.file.path),
            mode=config.mode,
            max_concurrency=config.max_concurrency,
            workspace_only=config.workspace_only,
            max_output_chars=config.max_output_chars,
            change=change,
        )

    def update_tool_execution(
        self,
        *,
        mode: ToolExecutionMode | None = None,
        max_concurrency: int | None = None,
        workspace_only: bool | None = None,
        max_output_chars: int | None = None,
    ) -> ToolExecutionConfigView:
        self.tool_execution.update(
            mode=mode,
            max_concurrency=max_concurrency,
            workspace_only=workspace_only,
            max_output_chars=max_output_chars,
        )
        return self.tool_execution_config(
            change=configuration_change("tool_execution", "updated", "next_runtime")
        )

    def queued_input_config(
        self,
        *,
        change: ConfigurationChange | None = None,
    ) -> QueuedInputConfigView:
        config = self.queued_inputs.snapshot()
        return QueuedInputConfigView(
            config_path=str(self.queued_inputs.file.path),
            consumption_mode=config.consumption_mode,
            change=change,
        )

    def update_queued_inputs(
        self,
        *,
        consumption_mode: QueuedInputConsumptionMode,
    ) -> QueuedInputConfigView:
        self.queued_inputs.update(consumption_mode=consumption_mode)
        return self.queued_input_config(
            change=configuration_change("queued_inputs", "updated", "next_runtime")
        )

    def run_budget_config(
        self,
        *,
        change: ConfigurationChange | None = None,
    ) -> RunBudgetConfigView:
        policy = self.run_budget.snapshot()
        return RunBudgetConfigView(
            config_path=str(self.run_budget.file.path),
            **policy.model_dump(),
            change=change,
        )

    def update_run_budget(
        self,
        *,
        max_model_rounds: int | None = None,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_elapsed_seconds: float | None = None,
        clear: bool = False,
    ) -> RunBudgetConfigView:
        self.run_budget.update(
            max_model_rounds=max_model_rounds,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            max_elapsed_seconds=max_elapsed_seconds,
            clear=clear,
        )
        return self.run_budget_config(
            change=configuration_change("run_budget", "updated", "next_runtime")
        )

    def compaction_config(
        self,
        *,
        change: ConfigurationChange | None = None,
    ) -> CompactionConfigView:
        return compaction_view(
            self.compaction.snapshot(),
            config_path=self.compaction.file.path,
            change=change,
        )

    def update_compaction_model(
        self,
        *,
        provider_id: str | None = None,
        endpoint_id: str | None = None,
        use_runtime_endpoint: bool = False,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        reasoning: ReasoningRequest | None = None,
        clear: bool = False,
    ) -> CompactionConfigView:
        config = self.compaction.update_model(
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            use_runtime_endpoint=use_runtime_endpoint,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            reasoning=reasoning,
            clear=clear,
        )
        return compaction_view(
            config,
            config_path=self.compaction.file.path,
            change=configuration_change("compaction", "model_updated", "next_runtime"),
        )

    def skill_policy(
        self,
        *,
        change: ConfigurationChange | None = None,
    ) -> SkillPolicyView:
        snapshot = self._skill_policy.snapshot()
        config = snapshot.config
        return SkillPolicyView(
            config_path=str(snapshot.config_path),
            enabled=config.enabled,
            disabled=sorted(config.disabled),
            paths=[item.model_dump(mode="json") for item in config.paths],
            scan_agents=config.scan_agents,
            scan_claude=config.scan_claude,
            scan_copilot=config.scan_copilot,
            scan_codex=config.scan_codex,
            scan_github=config.scan_github,
            max_depth=config.max_depth,
            max_directories=config.max_directories,
            installed=[
                managed_skill_view(name, record)
                for name, record in sorted(config.installed.items())
            ],
            change=change,
        )

    def skill_catalog(self, *, include_disabled: bool = False) -> SkillCatalogView:
        catalog = discover_skill_catalog(home=self.root, include_disabled=include_disabled)
        installed = self._skill_policy.snapshot().config.installed
        return SkillCatalogView(
            roots=self.skill_roots(),
            skills=[skill_view(skill, installed=installed) for skill in catalog.skills],
            diagnostics=[skill_diagnostic_view(item) for item in catalog.diagnostics],
        )

    def skill_roots(self) -> list[SkillRootView]:
        return [
            SkillRootView(
                path=str(root.path),
                source=root.source,
                origin=root.origin,
                exists=root.path.exists(),
            )
            for root in default_skill_roots(home=self.root)
        ]

    def skill(self, name: str) -> LoadedSkillView:
        catalog = discover_skill_catalog(home=self.root, include_disabled=True)
        metadata = next((item for item in catalog.skills if item.name == name), None)
        if metadata is None:
            raise ConfigError(f"skill not found: {name}")
        loader = SkillLoader(
            Path(metadata.location).parent,
            default_source=metadata.source,
        )
        loaded = loader.load_content(name)
        loaded = loaded.model_copy(
            update={"metadata": loaded.metadata.model_copy(update={"enabled": metadata.enabled})}
        )
        return loaded_skill_view(
            loaded,
            installed=self._skill_policy.snapshot().config.installed,
        )

    def skills_prompt(self) -> SkillsPromptView:
        skills = discover_skill_catalog(home=self.root).skills
        return SkillsPromptView(skill_count=len(skills), prompt=render_agent_skills_prompt(skills))

    def validate_skills(self, path: str | Path | None = None) -> SkillValidationView:
        if path is None:
            catalog = discover_skill_catalog(home=self.root, include_disabled=True)
            valid = bool(catalog.skills) and not any(
                item.level == "error" for item in catalog.diagnostics
            )
            return SkillValidationView(
                valid=valid,
                skills=[skill_view(item) for item in catalog.skills],
                diagnostics=[skill_diagnostic_view(item) for item in catalog.diagnostics],
            )
        result = self.skills.validate(path)
        return SkillValidationView(
            valid=result.valid,
            skills=[skill_view(item) for item in result.skills],
            diagnostics=[skill_diagnostic_view(item) for item in result.diagnostics],
        )

    def install_skills(
        self,
        source: str,
        *,
        scope: SkillInstallScope = "user",
        selector: str | None = None,
        install_all: bool = False,
        force: bool = False,
        ref: str | None = None,
    ) -> SkillManagementView:
        return skill_management_view(
            self.skills.install(
                source,
                scope=scope,
                selector=selector,
                install_all=install_all,
                force=force,
                ref=ref,
            )
        )

    def update_skill(self, name: str) -> SkillManagementView:
        return skill_management_view(self.skills.update(name))

    def remove_skill(self, name: str, *, force: bool = False) -> SkillManagementView:
        return skill_management_view(self.skills.remove(name, force=force))

    def set_skill_enabled(self, name: str, *, enabled: bool) -> SkillManagementView:
        return skill_management_view(self.skills.set_enabled(name, enabled=enabled))

    def _require_provider(self, provider_id: str) -> ProviderConfig:
        provider = self.providers.get_provider(provider_id, include_builtin=False)
        if provider is None:
            raise ConfigError(f"provider not found: {provider_id}")
        return provider
