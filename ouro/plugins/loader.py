from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from pydantic import Field

from ouro.core.base import Metadata, MetadataField, OuroModel
from ouro.core.compaction import CompactionStrategy, CompactionStrategyRegistry
from ouro.core.hooks import HookRegistration, HookRegistry, PluginSource
from ouro.core.redaction import redact_text
from ouro.core.tools import ToolSource
from ouro.plugins.manifest import (
    PluginCapability,
    PluginManifest,
    PluginPermissions,
    PluginStatus,
)
from ouro.plugins.registry import (
    PluginCommand,
    PluginCommandRegistry,
    PluginContextProvider,
    bind_plugin_command,
    context_provider_hook,
)
from ouro.tools import Tool, ToolRegistry


class DiscoveredPlugin(OuroModel):
    path: str
    manifest: PluginManifest | None = None
    status: PluginStatus = "discovered"
    enabled: bool = False
    source: PluginSource = "third_party"
    granted_permissions: PluginPermissions = Field(default_factory=PluginPermissions)
    error: str | None = None
    metadata: Metadata = MetadataField


class PluginRegistrationResult(OuroModel):
    plugin_id: str
    registered_tools: list[str] = Field(default_factory=list)
    registered_hooks: list[str] = Field(default_factory=list)
    registered_commands: list[str] = Field(default_factory=list)
    registered_context_providers: list[str] = Field(default_factory=list)
    registered_compaction_strategies: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


@dataclass
class PluginContributions:
    tools: list[Tool] = field(default_factory=list)
    hooks: list[HookRegistration] = field(default_factory=list)
    commands: list[PluginCommand] = field(default_factory=list)
    context_providers: list[PluginContextProvider] = field(default_factory=list)
    compaction_strategies: list[CompactionStrategy] = field(default_factory=list)


class PluginLoader:
    def __init__(
        self,
        root: str | Path,
        *,
        enabled_plugin_ids: Iterable[str] | None = None,
        default_enabled: bool = False,
        trusted_source: PluginSource = "third_party",
        permission_grants: Mapping[str, PluginPermissions] | None = None,
    ) -> None:
        self.root = Path(root)
        self.enabled_plugin_ids = set(enabled_plugin_ids or [])
        self.default_enabled = default_enabled
        self.trusted_source = trusted_source
        self.permission_grants = dict(permission_grants or {})

    def discover(self) -> list[DiscoveredPlugin]:
        manifests = list(self._manifest_paths())
        records = [self._load_manifest(path) for path in manifests]
        return sorted(records, key=lambda item: item.path)

    def register_contributions(
        self,
        plugin: DiscoveredPlugin,
        contributions: PluginContributions,
        *,
        tool_registry: ToolRegistry,
        hook_registry: HookRegistry,
        command_registry: PluginCommandRegistry | None = None,
        compaction_registry: CompactionStrategyRegistry | None = None,
    ) -> PluginRegistrationResult:
        if plugin.manifest is None:
            return PluginRegistrationResult(
                plugin_id="unknown",
                errors=[plugin.error or "plugin manifest is invalid"],
            )
        result = PluginRegistrationResult(plugin_id=plugin.manifest.plugin_id)
        if not plugin.enabled:
            result.errors.append(plugin.error or "plugin is disabled")
            return result

        capabilities = set(plugin.manifest.capabilities)

        if contributions.tools and _requires_capability(result, capabilities, "tools"):
            prepared_tools: list[Tool] = []
            try:
                for tool in contributions.tools:
                    permission_mode = tool.definition.permission_mode
                    if plugin.source != "official" and permission_mode == "auto":
                        permission_mode = "approval"
                    definition = tool.definition.model_copy(
                        update={
                            "source": _tool_source_for_plugin(plugin.source),
                            "permission_mode": permission_mode,
                            "metadata": {
                                **tool.definition.metadata,
                                "plugin_id": plugin.manifest.plugin_id,
                            },
                        }
                    )
                    prepared_tools.append(Tool(definition=definition, handler=tool.handler))
                tool_registry.replace((), prepared_tools)
                result.registered_tools.extend(tool.definition.name for tool in prepared_tools)
            except Exception as exc:  # noqa: BLE001 - plugin registration is an isolation boundary.
                result.errors.append(redact_text(str(exc)))

        if contributions.hooks and _requires_capability(result, capabilities, "runtime_hooks"):
            prepared_hooks: list[HookRegistration] = []
            declared_hooks = {spec.hook_name: spec for spec in plugin.manifest.runtime_hooks}
            try:
                for hook in contributions.hooks:
                    spec = declared_hooks.get(hook.hook_name)
                    if spec is None:
                        raise ValueError(
                            f"plugin hook is not declared in manifest: {hook.hook_name}"
                        )
                    required = set(spec.permissions_required)
                    requested = plugin.manifest.permissions.model_dump()
                    undeclared = sorted(name for name in required if not requested[name])
                    if undeclared:
                        raise ValueError(
                            "plugin hook requires undeclared permissions: " + ", ".join(undeclared)
                        )
                    maximum_timeout = 1.0 if hook.hook_name == "on_event" else 10.0
                    timeout = spec.timeout_seconds or hook.timeout_seconds
                    prepared_hooks.append(
                        replace(
                            hook,
                            plugin_id=plugin.manifest.plugin_id,
                            source=plugin.source,
                            priority=spec.priority,
                            timeout_seconds=(
                                min(timeout, maximum_timeout)
                                if timeout is not None
                                else maximum_timeout
                            ),
                        )
                    )
                hook_registry.register_many(tuple(prepared_hooks))
                result.registered_hooks.extend(
                    registration.hook_name for registration in prepared_hooks
                )
            except Exception as exc:  # noqa: BLE001 - plugin registration is an isolation boundary.
                result.errors.append(redact_text(str(exc)))

        if contributions.commands and _requires_capability(result, capabilities, "commands"):
            if command_registry is None:
                result.errors.append("plugin command registry is unavailable")
            else:
                try:
                    prepared_commands = tuple(
                        bind_plugin_command(
                            command,
                            plugin_id=plugin.manifest.plugin_id,
                            source=plugin.source,
                            maximum_timeout_seconds=5.0,
                        )
                        for command in contributions.commands
                    )
                    command_registry.register_many(prepared_commands)
                    result.registered_commands.extend(
                        command.command_id for command in prepared_commands
                    )
                except Exception as exc:  # noqa: BLE001 - plugin isolation boundary.
                    result.errors.append(redact_text(str(exc)))

        if contributions.context_providers and _requires_capability(
            result, capabilities, "context_providers"
        ):
            try:
                registrations = tuple(
                    HookRegistration(
                        plugin_id=plugin.manifest.plugin_id,
                        hook_name="after_context_build",
                        handler=context_provider_hook(provider),
                        source=plugin.source,
                        priority=provider.priority,
                        timeout_seconds=10.0,
                    )
                    for provider in contributions.context_providers
                )
                hook_registry.register_many(registrations)
                result.registered_context_providers.extend(
                    provider.provider_id for provider in contributions.context_providers
                )
            except Exception as exc:  # noqa: BLE001 - plugin registration is an isolation boundary.
                result.errors.append(redact_text(str(exc)))

        if contributions.compaction_strategies and _requires_capability(
            result, capabilities, "compaction_strategies"
        ):
            if compaction_registry is None:
                result.errors.append("plugin compaction strategy registry is unavailable")
            else:
                try:
                    for strategy in contributions.compaction_strategies:
                        if strategy.strategy_id.startswith("builtin."):
                            raise ValueError(
                                "plugin compaction strategy cannot use reserved id: "
                                f"{strategy.strategy_id}"
                            )
                    compaction_registry.replace((), contributions.compaction_strategies)
                    result.registered_compaction_strategies.extend(
                        strategy.strategy_id for strategy in contributions.compaction_strategies
                    )
                except Exception as exc:  # noqa: BLE001 - plugin isolation boundary.
                    result.errors.append(redact_text(str(exc)))

        return result

    def _manifest_paths(self) -> Iterable[Path]:
        direct_manifest = self.root / "plugin.json"
        if direct_manifest.exists():
            yield direct_manifest
        if not self.root.exists():
            return
        for child in self.root.iterdir():
            manifest = child / "plugin.json"
            if child.is_dir() and manifest.exists():
                yield manifest

    def _load_manifest(self, path: Path) -> DiscoveredPlugin:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            manifest = PluginManifest.model_validate(raw)
            requested_enabled = (
                self.default_enabled or manifest.plugin_id in self.enabled_plugin_ids
            )
            grant = self.permission_grants.get(manifest.plugin_id, PluginPermissions())
            missing_permissions = manifest.permissions.missing_from(grant)
            enabled = requested_enabled and not missing_permissions
            status: PluginStatus = (
                "needs_authorization"
                if requested_enabled and missing_permissions
                else "enabled"
                if enabled
                else "disabled"
            )
            return DiscoveredPlugin(
                path=str(path.parent),
                manifest=manifest,
                status=status,
                enabled=enabled,
                source=self.trusted_source,
                granted_permissions=grant,
                error=(
                    "plugin permissions require authorization: " + ", ".join(missing_permissions)
                    if missing_permissions
                    else None
                ),
                metadata={
                    "declared_source_type": manifest.source_type,
                    "effective_source_type": self.trusted_source,
                },
            )
        except Exception as exc:  # noqa: BLE001 - failed plugin metadata must not stop startup.
            return DiscoveredPlugin(
                path=str(path.parent),
                status="failed",
                error=redact_text(str(exc)),
            )


def _tool_source_for_plugin(source: PluginSource) -> ToolSource:
    if source == "official":
        return "official_plugin"
    if source == "ai_generated":
        return "ai_generated_plugin"
    return "third_party_plugin"


def _requires_capability(
    result: PluginRegistrationResult,
    capabilities: set[PluginCapability],
    required: PluginCapability,
) -> bool:
    if required in capabilities:
        return True
    result.errors.append(f"plugin contribution requires undeclared capability: {required}")
    return False
