from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pydantic import Field

from ouro.core.base import Metadata, MetadataField, OuroModel
from ouro.core.compaction import CompactionStrategy, CompactionStrategyRegistry
from ouro.core.hooks import HookRegistration, HookRegistry, PluginSource
from ouro.plugins.manifest import PluginManifest, PluginStatus
from ouro.plugins.registry import (
    PluginCommand,
    PluginCommandRegistry,
    PluginContextProvider,
    context_provider_hook,
)
from ouro.tools import Tool, ToolRegistry


class DiscoveredPlugin(OuroModel):
    path: str
    manifest: PluginManifest | None = None
    status: PluginStatus = "discovered"
    enabled: bool = False
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
    ) -> None:
        self.root = Path(root)
        self.enabled_plugin_ids = set(enabled_plugin_ids or [])
        self.default_enabled = default_enabled

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
            result.errors.append("plugin is disabled")
            return result

        for tool in contributions.tools:
            try:
                definition = tool.definition.model_copy(
                    update={"source": _tool_source_for_plugin(plugin.manifest.source_type)}
                )
                tool_registry.register(Tool(definition=definition, handler=tool.handler))
                result.registered_tools.append(definition.name)
            except Exception as exc:  # noqa: BLE001 - plugin registration is an isolation boundary.
                result.errors.append(str(exc))

        for hook in contributions.hooks:
            try:
                registration = replace(
                    hook,
                    plugin_id=plugin.manifest.plugin_id,
                    source=plugin.manifest.source_type,
                )
                hook_registry.register(registration)
                result.registered_hooks.append(registration.hook_name)
            except Exception as exc:  # noqa: BLE001 - plugin registration is an isolation boundary.
                result.errors.append(str(exc))

        for command in contributions.commands:
            if command_registry is None:
                result.errors.append(f"command registry is unavailable for {command.name}")
                continue
            try:
                registered = replace(
                    command,
                    plugin_id=plugin.manifest.plugin_id,
                    source=plugin.manifest.source_type,
                )
                command_registry.register(registered)
                result.registered_commands.append(registered.name)
            except Exception as exc:  # noqa: BLE001 - plugin registration is an isolation boundary.
                result.errors.append(str(exc))

        for provider in contributions.context_providers:
            try:
                registration = HookRegistration(
                    plugin_id=plugin.manifest.plugin_id,
                    hook_name="after_context_build",
                    handler=context_provider_hook(provider),
                    source=plugin.manifest.source_type,
                    priority=provider.priority,
                )
                hook_registry.register(registration)
                result.registered_context_providers.append(provider.provider_id)
            except Exception as exc:  # noqa: BLE001 - plugin registration is an isolation boundary.
                result.errors.append(str(exc))

        for strategy in contributions.compaction_strategies:
            if compaction_registry is None:
                result.errors.append(
                    f"compaction strategy registry is unavailable for {strategy.strategy_id}"
                )
                continue
            try:
                if strategy.strategy_id.startswith("builtin."):
                    raise ValueError(
                        f"plugin compaction strategy cannot use reserved id: {strategy.strategy_id}"
                    )
                compaction_registry.register(strategy)
                result.registered_compaction_strategies.append(strategy.strategy_id)
            except Exception as exc:  # noqa: BLE001 - plugin registration isolation boundary.
                result.errors.append(str(exc))

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
            enabled = self.default_enabled or manifest.plugin_id in self.enabled_plugin_ids
            return DiscoveredPlugin(
                path=str(path.parent),
                manifest=manifest,
                status="enabled" if enabled else "disabled",
                enabled=enabled,
            )
        except Exception as exc:  # noqa: BLE001 - failed plugin metadata must not stop startup.
            return DiscoveredPlugin(
                path=str(path.parent),
                status="failed",
                error=str(exc),
            )


def _tool_source_for_plugin(source: PluginSource) -> Any:
    if source == "official":
        return "official_plugin"
    if source == "ai_generated":
        return "ai_generated_plugin"
    return "third_party_plugin"
