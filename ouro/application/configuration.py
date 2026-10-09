from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ouro.application.configuration_models import (
    AgentRuntimeConfigView,
    CompactionConfigInput,
    CompactionConfigView,
    ConfigurationChange,
    ConfigurationOverview,
    LoadedSkillView,
    MCPConfigCatalogView,
    MCPConnectionCheckView,
    MCPServerConfigInput,
    MCPServerConfigView,
    PermissionConfigView,
    ProviderCatalogView,
    ProviderEndpointCatalogView,
    ProviderEndpointView,
    ProviderTemplateCatalogView,
    ProviderTemplateUpdateView,
    ProviderTemplateView,
    ProviderView,
    QueuedInputConfigView,
    RunBudgetConfigView,
    SkillCatalogView,
    SkillManagementView,
    SkillPolicyView,
    SkillRootView,
    SkillSourcePolicyView,
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
    mcp_server_view,
    provider_template_update_view,
    provider_template_view,
    provider_view,
    skill_diagnostic_view,
    skill_management_view,
    skill_view,
)
from ouro.application.dto import Diagnostic
from ouro.config import (
    BUILTIN_FAUX_PROVIDER_ID,
    INTEGRATED_PROVIDER_TYPES,
    AgentDefinitionsConfigManager,
    AgentRuntimeConfig,
    CompactionConfig,
    CompactionConfigManager,
    CompactionModelConfig,
    EndpointReasoningConfig,
    PermissionConfigManager,
    PromptConfigManager,
    ProviderConfig,
    ProviderConfigManager,
    ProviderProtocol,
    ProviderTemplateEntry,
    ProviderTemplateStore,
    ProviderType,
    QueuedInputConfigManager,
    QueuedInputConsumptionMode,
    RunBudgetConfigManager,
    ToolExecutionConfigManager,
)
from ouro.config.prompts import PromptConfigSnapshot, PromptDraft, PromptKind
from ouro.config.skills import SKILL_DISCOVERY_SOURCE_IDS, SkillInstallScope
from ouro.core.agents import ExecutionMode, LifecycleBinding
from ouro.core.compaction import CompactionPolicy
from ouro.core.context import compose_system_prompt
from ouro.core.errors import ConfigError
from ouro.core.provider import ProviderRetryPolicy
from ouro.core.reasoning import ReasoningRequest, resolve_reasoning
from ouro.core.redaction import redact_text
from ouro.core.tool_batches import ToolExecutionMode
from ouro.core.tools import PermissionMode
from ouro.mcp import (
    MCPConfigStore,
    MCPServerConfigUpdate,
    check_mcp_server_connection,
)
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
        self.provider_templates = ProviderTemplateStore(root)
        self.providers = ProviderConfigManager(root)
        self.prompts = PromptConfigManager(root)
        self.permissions = PermissionConfigManager(root)
        self.tool_execution = ToolExecutionConfigManager(root)
        self.queued_inputs = QueuedInputConfigManager(root)
        self.run_budget = RunBudgetConfigManager(root)
        self.agents = AgentDefinitionsConfigManager(root)
        self.compaction = CompactionConfigManager(root)
        self.mcp = MCPConfigStore(root)
        self.skills = SkillManager(root)
        self._skill_policy = self.skills.config
        self.root = self.providers.root
        self.config_path = self.root / "config" / "config.json"

    def overview(self) -> ConfigurationOverview:
        return ConfigurationOverview(
            config_path=str(self.config_path),
            providers=self.provider_catalog(),
            prompt=self.system_prompt(),
            permissions=self.permission_config(),
            tool_execution=self.tool_execution_config(),
            queued_inputs=self.queued_input_config(),
            run_budget=self.run_budget_config(),
            agents=self.agent_runtime_config(),
            compaction=self.compaction_config(),
            skills=self.skill_policy(),
            mcp=self.mcp_catalog(),
        )

    def mcp_catalog(
        self,
        *,
        change: ConfigurationChange | None = None,
    ) -> MCPConfigCatalogView:
        snapshot = self.mcp.snapshot()
        return MCPConfigCatalogView(
            config_path=str(snapshot.config_path),
            servers=[mcp_server_view(config) for config in snapshot.servers],
            change=change,
        )

    def mcp_server(self, server_id: str) -> MCPServerConfigView:
        config = self.mcp.get(server_id)
        if config is None:
            raise ConfigError("MCP server not found")
        return mcp_server_view(config)

    def mcp_server_by_name(self, name: str) -> MCPServerConfigView:
        config = self.mcp.find_by_name(name)
        if config is None:
            raise ConfigError(f"MCP server not found: {name}")
        return mcp_server_view(config)

    def set_mcp_server_by_name(
        self, name: str, update: MCPServerConfigUpdate
    ) -> MCPServerConfigView:
        return mcp_server_view(self.mcp.set_by_name(name, update))

    def save_mcp_server(
        self,
        server_id: str | None,
        update: MCPServerConfigUpdate | MCPServerConfigInput,
    ) -> MCPServerConfigView:
        if isinstance(update, MCPServerConfigInput):
            update = MCPServerConfigUpdate.model_validate(
                update.model_dump(mode="python", exclude_unset=True)
            )
        return mcp_server_view(self.mcp.update(server_id, update))

    def remove_mcp_server(self, server_id: str) -> MCPConfigCatalogView:
        self.mcp.remove(server_id)
        return self.mcp_catalog(change=configuration_change("mcp", "removed", "next_runtime"))

    async def check_mcp_server(self, server_id: str) -> MCPConnectionCheckView:
        config = self.mcp.get(server_id)
        if config is None:
            raise ConfigError("MCP server not found")
        result = await check_mcp_server_connection(config)
        return MCPConnectionCheckView(
            ok=result.ok,
            status=result.status,
            server=mcp_server_view(config),
            server_info=(
                result.server_info.model_dump(mode="json")
                if result.server_info is not None
                else None
            ),
            tool_names=result.tool_names,
            error=redact_text(result.error) if result.error is not None else None,
            diagnostics=tuple(
                Diagnostic(
                    code="mcp_connection_diagnostic",
                    severity=(
                        "error"
                        if item.level == "error"
                        else "warning"
                        if item.level == "warning"
                        else "info"
                    ),
                    component="mcp.connection",
                    message=redact_text(item.message),
                    occurred_at=item.created_at,
                    recoverable=True,
                )
                for item in result.diagnostics
            ),
        )

    def provider_catalog(
        self,
        *,
        change: ConfigurationChange | None = None,
        changed_provider_id: str | None = None,
        template_update: ProviderTemplateUpdateView | None = None,
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
            changed_provider_id=changed_provider_id,
            providers=[
                provider_view(
                    provider,
                    is_default=provider.provider_id == snapshot.default_provider_id,
                )
                for provider in snapshot.providers
            ],
            change=change,
            template_update=template_update,
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
        snapshot = self.provider_templates.catalog()
        return ProviderTemplateCatalogView(
            templates=[
                provider_template_view(entry.template, deprecated=entry.deprecated)
                for entry in snapshot.entries
                if entry.enabled
            ],
            notices=list(snapshot.notices),
        )

    def provider_template(self, template_id: str) -> ProviderTemplateView:
        entry = self.provider_templates.get_entry(template_id)
        return provider_template_view(entry.template, deprecated=entry.deprecated)

    def restore_provider_template(self, template_id: str) -> ProviderTemplateView:
        return provider_template_view(self.provider_templates.restore(template_id))

    def add_provider(
        self,
        *,
        display_name: str,
        provider_type: ProviderType,
        protocol: ProviderProtocol | None = None,
        auth_driver: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        headers: Mapping[str, Any] | None = None,
        provider_options: Mapping[str, Any] | None = None,
        retry_max_attempts: int | None = None,
        retry_initial_backoff_seconds: float | None = None,
        retry_max_backoff_seconds: float | None = None,
        retry_backoff_multiplier: float | None = None,
        enabled: bool = True,
        make_default: bool = False,
    ) -> ProviderCatalogView:
        retry_policy = self._resolve_provider_retry_policy(
            None,
            preserve_current=False,
            retry_policy=retry_policy,
            max_attempts=retry_max_attempts,
            initial_backoff_seconds=retry_initial_backoff_seconds,
            max_backoff_seconds=retry_max_backoff_seconds,
            backoff_multiplier=retry_backoff_multiplier,
        )
        if provider_type in INTEGRATED_PROVIDER_TYPES:
            entry = self.provider_templates.get_entry(provider_type)
            self._validate_template_protocol(entry, protocol)
            created = self.providers.create_from_template(
                entry.template,
                auth_driver=auth_driver,
                template_origin=entry.origin,
                display_name=display_name,
                model_id=model,
                base_url=base_url,
                api_path=api_path,
                api_key=api_key,
                api_key_env=api_key_env,
                timeout_seconds=timeout,
                retry_policy=retry_policy,
                headers=headers,
                provider_options=provider_options,
                enabled=enabled,
                make_default=make_default,
            )
        else:
            self._reject_template_options(
                provider_type,
                model=model,
                make_default=make_default,
            )
            created = self.providers.create_provider(
                display_name=display_name,
                protocol=protocol,
                auth_driver=auth_driver,
                provider_type=provider_type,
                base_url=base_url,
                api_path=api_path,
                api_key=api_key,
                api_key_env=api_key_env,
                timeout_seconds=timeout,
                retry_policy=retry_policy,
                headers=headers,
                provider_options=provider_options,
                enabled=enabled,
            )
        return self.provider_catalog(
            change=configuration_change("providers", "created", "next_request"),
            changed_provider_id=created.provider_id,
        )

    def update_provider(
        self,
        provider_id: str,
        *,
        display_name: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        headers: Mapping[str, Any] | None = None,
        provider_options: Mapping[str, Any] | None = None,
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
            entry = self._provider_template_for_update(current)
            result = self.providers.update_from_template(
                entry.template,
                template_origin=entry.origin,
                provider_id=provider_id,
                display_name=display_name,
                model_id=model,
                base_url=base_url,
                api_path=api_path,
                api_key=api_key,
                api_key_env=api_key_env,
                timeout_seconds=timeout,
                retry_policy=retry_policy,
                headers=headers,
                provider_options=provider_options,
                enabled=enabled,
                make_default=make_default,
            )
            template_update = provider_template_update_view(result)
        else:
            self._reject_template_options(
                current.provider_type,
                model=model,
                make_default=False,
            )
            self.providers.update_provider(
                provider_id,
                display_name=display_name,
                base_url=base_url,
                api_path=api_path,
                api_key=api_key,
                api_key_env=api_key_env,
                timeout_seconds=timeout,
                retry_policy=retry_policy,
                headers=headers,
                provider_options=provider_options,
                enabled=enabled,
                make_default=make_default,
            )
            template_update = None
        return self.provider_catalog(
            change=configuration_change("providers", "updated", "next_request"),
            changed_provider_id=provider_id,
            template_update=template_update,
        )

    def update_provider_settings(
        self,
        provider_id: str,
        *,
        display_name: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        headers: Mapping[str, Any] | None = None,
        provider_options: Mapping[str, Any] | None = None,
        enabled: bool | None = None,
        make_default: bool = False,
    ) -> ProviderCatalogView:
        """Update supplier-level settings without reapplying endpoint templates.

        Template synchronization intentionally refreshes integrated endpoints.
        Hosts editing connection details need a narrower contract so credentials,
        routing, and availability cannot silently rewrite model endpoints.
        """

        self.providers.update_provider(
            provider_id,
            display_name=display_name,
            base_url=base_url,
            api_path=api_path,
            api_key=api_key,
            api_key_env=api_key_env,
            timeout_seconds=timeout,
            retry_policy=retry_policy,
            headers=headers,
            provider_options=provider_options,
            enabled=enabled,
            make_default=make_default,
        )
        return self.provider_catalog(
            change=configuration_change("providers", "settings_updated", "next_request"),
            changed_provider_id=provider_id,
        )

    def replace_provider(
        self,
        provider_id: str,
        *,
        display_name: str,
        provider_type: ProviderType,
        protocol: ProviderProtocol | None = None,
        auth_driver: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        headers: Mapping[str, Any] | None = None,
        provider_options: Mapping[str, Any] | None = None,
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
            entry = self.provider_templates.get_entry(provider_type)
            self._validate_template_protocol(entry, protocol)
            self.providers.replace_from_template(
                entry.template,
                auth_driver=auth_driver,
                template_origin=entry.origin,
                provider_id=provider_id,
                display_name=display_name,
                model_id=model,
                base_url=base_url,
                api_path=api_path,
                api_key=api_key,
                api_key_env=api_key_env,
                timeout_seconds=timeout,
                retry_policy=retry_policy,
                headers=headers,
                provider_options=provider_options,
                enabled=enabled,
                make_default=make_default,
            )
        else:
            self._reject_template_options(
                provider_type,
                model=model,
                make_default=make_default,
            )
            self.providers.replace_provider(
                provider_id,
                display_name=display_name,
                protocol=protocol,
                auth_driver=auth_driver,
                provider_type=provider_type,
                base_url=base_url,
                api_path=api_path,
                api_key=api_key,
                api_key_env=api_key_env,
                timeout_seconds=timeout,
                retry_policy=retry_policy,
                headers=headers,
                provider_options=provider_options,
                enabled=enabled,
            )
        return self.provider_catalog(
            change=configuration_change("providers", "replaced", "next_request"),
            changed_provider_id=provider_id,
        )

    @staticmethod
    def _validate_template_protocol(
        entry: ProviderTemplateEntry,
        protocol: ProviderProtocol | None,
    ) -> None:
        if protocol is not None and protocol != entry.template.protocol:
            raise ConfigError(
                f"protocol does not match template {entry.template.template_id}: "
                f"{protocol} != {entry.template.protocol}; "
                "use provider type custom with an explicit protocol"
            )

    def _resolve_provider_retry_policy(
        self,
        provider_id: str | None,
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
        current = (
            self.providers.get_provider(provider_id)
            if preserve_current and provider_id is not None
            else None
        )
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
        model: str | None,
        make_default: bool,
    ) -> None:
        if model is not None:
            raise ConfigError(
                "--model requires an integrated provider type; "
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

    def add_endpoint(
        self,
        provider_id: str,
        *,
        display_name: str | None = None,
        model: str,
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
        created = self.providers.create_endpoint(
            provider_id,
            display_name=display_name,
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
            change=configuration_change("providers", "endpoint_created", "next_request"),
            changed_endpoint_id=created.endpoint_id,
        )

    def update_endpoint(
        self,
        provider_id: str,
        endpoint_id: str,
        *,
        display_name: str | None,
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
        updated = self.providers.update_endpoint(
            provider_id,
            endpoint_id,
            display_name=display_name,
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
            change=configuration_change("providers", "endpoint_updated", "next_request"),
            changed_endpoint_id=updated.endpoint_id,
        )

    def use_endpoint(self, provider_id: str, endpoint_id: str) -> ProviderEndpointCatalogView:
        self.providers.set_default_route(provider_id, endpoint_id)
        return endpoint_catalog_view(
            self._require_provider(provider_id),
            change=configuration_change("providers", "default_route_changed", "next_request"),
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
        return self._prompt_view(snapshot, change=change)

    @staticmethod
    def _prompt_view(
        snapshot: PromptConfigSnapshot, *, change: ConfigurationChange | None = None
    ) -> SystemPromptConfigView:
        config = snapshot.config
        base = config.resolve_system_prompt()
        additional = config.additional_system_prompt
        return SystemPromptConfigView(
            config_path=str(snapshot.config_path),
            config=config,
            configured=config.system_prompt is not None,
            system_prompt=config.system_prompt,
            additional_system_prompt=additional,
            effective_system_prompt=compose_system_prompt(base, additional),
            change=change,
        )

    def save_prompt(
        self,
        kind: PromptKind,
        draft: PromptDraft,
        *,
        entry_id: str | None = None,
        expected_revision: int,
    ) -> SystemPromptConfigView:
        snapshot = self.prompts.save_entry(
            kind, draft, entry_id=entry_id, expected_revision=expected_revision
        )
        return self._prompt_view(
            snapshot, change=configuration_change("prompts", "saved", "next_request")
        )

    def remove_prompt(
        self, kind: PromptKind, entry_id: str, *, expected_revision: int
    ) -> SystemPromptConfigView:
        snapshot = self.prompts.remove_entry(kind, entry_id, expected_revision=expected_revision)
        return self._prompt_view(
            snapshot, change=configuration_change("prompts", "removed", "next_request")
        )

    def set_prompt_enabled(
        self, kind: PromptKind, entry_id: str, enabled: bool, *, expected_revision: int
    ) -> SystemPromptConfigView:
        snapshot = self.prompts.set_enabled(
            kind, entry_id, enabled, expected_revision=expected_revision
        )
        return self._prompt_view(
            snapshot, change=configuration_change("prompts", "saved", "next_request")
        )

    def move_prompt(
        self, kind: PromptKind, entry_id: str, position: int, *, expected_revision: int
    ) -> SystemPromptConfigView:
        snapshot = self.prompts.move_entry(
            kind, entry_id, position, expected_revision=expected_revision
        )
        return self._prompt_view(
            snapshot, change=configuration_change("prompts", "reordered", "next_request")
        )

    def permission_config(
        self,
        *,
        change: ConfigurationChange | None = None,
    ) -> PermissionConfigView:
        snapshot = self.permissions.snapshot()
        return PermissionConfigView(
            config_path=str(snapshot.config_path),
            tools=snapshot.tools,
            plugins=snapshot.plugins,
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

    def set_plugin_permission(self, plugin_id: str, mode: PermissionMode) -> PermissionConfigView:
        self.permissions.set_plugin_mode(plugin_id, mode)
        return self.permission_config(
            change=configuration_change("permissions", "plugin_mode_saved", "next_runtime")
        )

    def clear_plugin_permission(self, plugin_id: str) -> PermissionConfigView:
        self.permissions.clear_plugin_mode(plugin_id)
        return self.permission_config(
            change=configuration_change("permissions", "plugin_mode_cleared", "next_runtime")
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

    def agent_runtime_config(
        self,
        *,
        change: ConfigurationChange | None = None,
    ) -> AgentRuntimeConfigView:
        snapshot = self.agents.snapshot()
        return AgentRuntimeConfigView(
            config_path=str(self.agents.file.path),
            revision=snapshot.revision,
            **snapshot.runtime.model_dump(),
            change=change,
        )

    def update_agent_runtime(
        self,
        *,
        expected_revision: int,
        max_depth: int | None = None,
        allowed_agent_id_patterns: tuple[str, ...] | None = None,
        allowed_model_patterns: tuple[str, ...] | None = None,
        allowed_execution_modes: tuple[ExecutionMode, ...] | None = None,
        allowed_lifecycle_bindings: tuple[LifecycleBinding, ...] | None = None,
        allow_write: bool | None = None,
        allow_network: bool | None = None,
        allow_mcp: bool | None = None,
        allow_external_paths: bool | None = None,
    ) -> AgentRuntimeConfigView:
        current = self.agents.snapshot()
        policy = current.runtime.spawn_policy
        policy_updates = {
            name: value
            for name, value in (
                ("allowed_agent_id_patterns", allowed_agent_id_patterns),
                ("allowed_model_patterns", allowed_model_patterns),
                ("allowed_execution_modes", allowed_execution_modes),
                ("allowed_lifecycle_bindings", allowed_lifecycle_bindings),
                ("allow_write", allow_write),
                ("allow_network", allow_network),
                ("allow_mcp", allow_mcp),
                ("allow_external_paths", allow_external_paths),
            )
            if value is not None
        }
        if policy_updates:
            policy = policy.model_copy(update=policy_updates)
        updates: dict[str, object] = {"spawn_policy": policy}
        if max_depth is not None:
            updates["max_depth"] = max_depth
        try:
            runtime = AgentRuntimeConfig.model_validate(
                current.runtime.model_copy(update=updates).model_dump()
            )
        except ValueError as exc:
            raise ConfigError(f"invalid Agent runtime config: {exc}") from exc
        self.agents.update_runtime(runtime, expected_revision=expected_revision)
        return self.agent_runtime_config(
            change=configuration_change("agents", "runtime_updated", "next_runtime")
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
        use_default_model: bool = False,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        reasoning: ReasoningRequest | None = None,
        clear: bool = False,
    ) -> CompactionConfigView:
        config = self.compaction.update_model(
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            use_runtime_endpoint=use_runtime_endpoint,
            use_default_model=use_default_model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            reasoning=reasoning,
            clear=clear,
            validate=self._validate_compaction_model_capabilities,
        )
        return compaction_view(
            config,
            config_path=self.compaction.file.path,
            change=configuration_change("compaction", "model_updated", "next_runtime"),
        )

    def save_compaction(self, draft: CompactionConfigInput) -> CompactionConfigView:
        """Atomically replace the complete compaction policy and model configuration."""

        try:
            config = CompactionConfig(
                strategy_id=draft.strategy_id,
                fallback_strategy_id=draft.fallback_strategy_id,
                policy=CompactionPolicy(
                    auto_enabled=draft.auto_enabled,
                    trigger_ratio=draft.trigger_ratio,
                    target_ratio=draft.target_ratio,
                ),
                model=CompactionModelConfig(
                    provider_id=draft.provider_id,
                    endpoint_id=draft.endpoint_id,
                    use_default_model=draft.use_default_model,
                    temperature=draft.temperature,
                    max_output_tokens=draft.max_output_tokens,
                    reasoning=draft.reasoning,
                ),
            )
            self._validate_compaction_model_capabilities(config.model)
        except ValueError as exc:
            raise ConfigError(f"invalid compaction config: {exc}") from exc
        saved = self.compaction.save(config)
        return compaction_view(
            saved,
            config_path=self.compaction.file.path,
            change=configuration_change("compaction", "updated", "next_runtime"),
        )

    def _validate_compaction_model_capabilities(self, model: CompactionModelConfig) -> None:
        if model.use_default_model:
            provider_id, endpoint_id = self.providers.require_default_route().split("/", 1)
        elif model.has_dedicated_endpoint:
            assert model.provider_id is not None
            assert model.endpoint_id is not None
            provider_id, endpoint_id = model.provider_id, model.endpoint_id
        else:
            return
        endpoint = self.providers.get_endpoint(provider_id, endpoint_id)
        if endpoint is None:
            return
        if (
            model.max_output_tokens is not None
            and model.max_output_tokens > endpoint.max_output_tokens
        ):
            raise ConfigError(
                "compaction max_output_tokens exceed the selected endpoint output limit"
            )
        if model.reasoning is None:
            return
        try:
            resolve_reasoning(endpoint.reasoning.capability, model.reasoning)
        except ValueError as exc:
            raise ConfigError(f"invalid compaction reasoning override: {exc}") from exc

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
            disabled_sources=sorted(config.disabled_sources),
            sources=[
                SkillSourcePolicyView(
                    source_id=source_id,
                    origin=source_id.rsplit(".", 1)[0],
                    scope="user" if source_id.endswith(".user") else "workspace",
                    enabled=source_id not in config.disabled_sources,
                )
                for source_id in SKILL_DISCOVERY_SOURCE_IDS
            ],
            paths=[item.model_dump(mode="json") for item in config.paths],
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
                source_id=root.source_id,
                origin=root.origin,
                enabled=root.enabled,
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
            default_origin=metadata.origin,
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

    def update_skill_discovery(
        self,
        *,
        enabled: bool | None = None,
        source_updates: Mapping[str, bool] | None = None,
    ) -> SkillPolicyView:
        self._skill_policy.update_discovery(
            enabled=enabled,
            source_updates=source_updates,
        )
        return self.skill_policy(
            change=configuration_change("skills", "discovery_updated", "next_runtime")
        )

    def _require_provider(self, provider_id: str) -> ProviderConfig:
        provider = self.providers.get_provider(provider_id, include_builtin=False)
        if provider is None:
            raise ConfigError(f"provider not found: {provider_id}")
        return provider

    def _provider_template_for_update(
        self,
        provider: ProviderConfig,
    ) -> ProviderTemplateEntry:
        template_id = (
            provider.template.template_id
            if provider.template is not None
            else provider.provider_type
        )
        entry = self.provider_templates.get_entry(template_id)
        if entry.deprecated:
            raise ConfigError(
                f"provider template was removed from OURO: {template_id}; "
                "the provider was left unchanged"
            )
        if not entry.enabled:
            raise ConfigError(
                f"provider template is disabled: {template_id}; "
                "enable it before updating the provider"
            )
        return entry
