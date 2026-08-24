from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ouro.application.configuration_models import (
    CompactionConfigView,
    CompactionModelConfigView,
    ConfigurationChange,
    ConfigurationEffectiveFrom,
    ConfigurationSection,
    CredentialStatus,
    LoadedSkillView,
    ManagedSkillResultView,
    ManagedSkillView,
    MCPServerConfigView,
    ProviderEndpointCatalogView,
    ProviderEndpointView,
    ProviderTemplateView,
    ProviderView,
    SkillDiagnosticView,
    SkillManagementView,
    SkillView,
)
from ouro.config import (
    CompactionConfig,
    CredentialResolver,
    ProviderConfig,
    ProviderEndpointConfig,
    ProviderTemplate,
    endpoint_config_to_public_dict,
)
from ouro.config.endpoints import redact_sensitive_mapping
from ouro.core.errors import CredentialError
from ouro.mcp import MCPServerConfig, mcp_config_to_public_dict
from ouro.skills import LoadedSkill, SkillDiagnostic, SkillManagementResult, SkillMetadata


def configuration_change(
    section: ConfigurationSection,
    action: str,
    effective_from: ConfigurationEffectiveFrom,
) -> ConfigurationChange:
    return ConfigurationChange(
        section=section,
        action=action,
        effective_from=effective_from,
    )


def mcp_server_view(config: MCPServerConfig) -> MCPServerConfigView:
    public = mcp_config_to_public_dict(config)
    public.pop("metadata", None)
    return MCPServerConfigView.model_validate(public)


def provider_view(provider: ProviderConfig, *, is_default: bool) -> ProviderView:
    return ProviderView(
        provider_id=provider.provider_id,
        provider_type=provider.provider_type,
        protocol=provider.protocol,
        credential=credential_status(provider),
        enabled=provider.enabled,
        timeout=provider.timeout_seconds,
        retry_policy=provider.retry_policy,
        base_url=provider.base_url,
        api_path=provider.api_path,
        provider_options=redact_sensitive_mapping(provider.extra),
        default=is_default,
        default_endpoint=provider.default_endpoint,
        endpoints=[
            endpoint_view(endpoint, default_endpoint=provider.default_endpoint)
            for endpoint in provider.endpoints
        ],
    )


def endpoint_view(
    endpoint: ProviderEndpointConfig,
    *,
    default_endpoint: str | None,
) -> ProviderEndpointView:
    return ProviderEndpointView.model_validate(
        {
            **endpoint_config_to_public_dict(endpoint),
            "default": endpoint.endpoint_id == default_endpoint,
        }
    )


def endpoint_catalog_view(
    provider: ProviderConfig,
    *,
    change: ConfigurationChange | None = None,
) -> ProviderEndpointCatalogView:
    return ProviderEndpointCatalogView(
        provider_id=provider.provider_id,
        default_endpoint=provider.default_endpoint,
        endpoints=[
            endpoint_view(endpoint, default_endpoint=provider.default_endpoint)
            for endpoint in provider.endpoints
        ],
        change=change,
    )


def provider_template_view(
    template: ProviderTemplate,
    *,
    deprecated: bool = False,
) -> ProviderTemplateView:
    return ProviderTemplateView(
        template_id=template.template_id,
        display_name=template.display_name,
        description=template.description,
        provider_id=template.provider_id,
        provider_type=template.provider_type,
        protocol=template.protocol,
        credential_env=template.credential_env,
        base_url=template.base_url,
        api_path=template.api_path,
        default_endpoint=template.default_endpoint,
        endpoints=[
            endpoint_view(endpoint, default_endpoint=template.default_endpoint)
            for endpoint in template.endpoints
        ],
        documentation_url=template.documentation_url,
        verified_on=template.verified_on,
        deprecated=deprecated,
    )


def credential_status(provider: ProviderConfig) -> CredentialStatus:
    reference = provider.credential_ref.display()
    if provider.protocol == "faux":
        return CredentialStatus(
            reference=reference,
            state="not_required",
            configured=False,
            available=True,
        )
    configured = provider.credential_ref.type != "none"
    available = False
    if configured:
        try:
            available = CredentialResolver().resolve(provider.credential_ref) is not None
        except CredentialError:
            available = False
    return CredentialStatus(
        reference=reference,
        state="configured" if available else "missing",
        configured=configured,
        available=available,
    )


def compaction_view(
    config: CompactionConfig,
    *,
    config_path: Path,
    change: ConfigurationChange | None = None,
) -> CompactionConfigView:
    return CompactionConfigView(
        config_path=str(config_path),
        strategy_id=config.strategy_id,
        fallback_strategy_id=config.fallback_strategy_id,
        policy=config.policy.model_dump(mode="json"),
        model=CompactionModelConfigView.model_validate(config.model.model_dump()),
        change=change,
    )


def managed_skill_view(name: str, record: Any) -> ManagedSkillView:
    return ManagedSkillView(name=name, **record.model_dump())


def skill_view(
    skill: SkillMetadata,
    *,
    installed: Mapping[str, Any] | None = None,
) -> SkillView:
    installed = installed or {}
    record = installed.get(skill.name)
    return SkillView(
        **skill.model_dump(),
        managed=record is not None,
        install=managed_skill_view(skill.name, record) if record is not None else None,
    )


def loaded_skill_view(
    loaded: LoadedSkill,
    *,
    installed: Mapping[str, Any],
) -> LoadedSkillView:
    return LoadedSkillView(
        skill=skill_view(loaded.metadata, installed=installed),
        content=loaded.content,
        resources=loaded.resources,
    )


def skill_diagnostic_view(item: SkillDiagnostic) -> SkillDiagnosticView:
    return SkillDiagnosticView.model_validate(item.model_dump())


def skill_management_view(result: SkillManagementResult) -> SkillManagementView:
    return SkillManagementView(
        action=result.action,
        skills=[
            ManagedSkillResultView(
                name=item.name,
                target=str(item.target),
                source=item.source,
                scope=item.scope,
            )
            for item in result.skills
        ],
        diagnostics=[skill_diagnostic_view(item) for item in result.diagnostics],
        change=configuration_change("skills", result.action, "next_runtime"),
    )
