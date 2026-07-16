from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Protocol

from ouro.core.cancellation import AbortSignal
from ouro.core.compaction import (
    CompactionArtifact,
    CompactionRequest,
    CompactionStrategy,
    CompactionStrategyRegistry,
)
from ouro.core.continuations import (
    ContinuationPolicyRegistration,
    RunContinuationRegistry,
)
from ouro.core.hooks import HookRegistration, HookRegistry, PluginSource
from ouro.core.tool_policies import ToolPolicyRegistration, ToolPolicyRegistry
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.models import InstalledPlugin, PluginGenerationRecord
from ouro.plugins.registry import (
    PluginCommand,
    PluginCommandRegistry,
    PluginContextProvider,
    bind_plugin_command,
    context_provider_hook,
)
from ouro.tools import Tool, ToolRegistry


class BackgroundService(Protocol):
    service_id: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


@dataclass
class PluginContributions:
    tools: list[Tool] = field(default_factory=list)
    hooks: list[HookRegistration] = field(default_factory=list)
    commands: list[PluginCommand] = field(default_factory=list)
    context_providers: list[PluginContextProvider] = field(default_factory=list)
    compaction_strategies: list[CompactionStrategy] = field(default_factory=list)
    background_services: list[BackgroundService] = field(default_factory=list)
    continuation_policies: list[ContinuationPolicyRegistration] = field(default_factory=list)
    tool_policies: list[ToolPolicyRegistration] = field(default_factory=list)


@dataclass(frozen=True)
class ContributionOwner:
    plugin_id: str
    package_digest: str
    generation_id: str

    @property
    def key(self) -> str:
        return f"{self.plugin_id}@{self.package_digest}#{self.generation_id}"


@dataclass(frozen=True)
class PreparedContributionSet:
    owner: ContributionOwner
    tools: tuple[Tool, ...] = ()
    hooks: tuple[HookRegistration, ...] = ()
    commands: tuple[PluginCommand, ...] = ()
    compaction_strategies: tuple[CompactionStrategy, ...] = ()
    background_services: tuple[BackgroundService, ...] = ()
    continuation_policies: tuple[ContinuationPolicyRegistration, ...] = ()
    tool_policies: tuple[ToolPolicyRegistration, ...] = ()
    contract_hashes: dict[str, str] = field(default_factory=dict)


class ContributionValidationError(ValueError):
    pass


@dataclass
class _BoundCompactionStrategy:
    strategy_id: str
    version: str
    delegate: CompactionStrategy

    async def compact(
        self,
        request: CompactionRequest,
        *,
        signal: AbortSignal | None = None,
    ) -> CompactionArtifact:
        return await self.delegate.compact(request, signal=signal)


class ContributionLease:
    def __init__(self, coordinator: ContributionCoordinator, owner: ContributionOwner) -> None:
        self._coordinator = coordinator
        self.owner = owner
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._coordinator.unregister(self.owner)

    def __enter__(self) -> ContributionLease:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class ContributionCoordinator:
    """Validate and atomically publish one generation across extension registries."""

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        hooks: HookRegistry,
        commands: PluginCommandRegistry | None = None,
        compaction: CompactionStrategyRegistry | None = None,
        continuations: RunContinuationRegistry | None = None,
        tool_policies: ToolPolicyRegistry | None = None,
    ) -> None:
        self.tools = tools
        self.hooks = hooks
        self.commands = commands
        self.compaction = compaction
        self.continuations = continuations
        self.tool_policies = tool_policies
        self._lock = threading.RLock()
        self._owners: set[str] = set()

    def prepare(
        self,
        installed: InstalledPlugin,
        generation: PluginGenerationRecord,
        contributions: PluginContributions,
        *,
        static_hooks: tuple[HookRegistration, ...] = (),
        discovered_tools: tuple[Tool, ...] = (),
    ) -> PreparedContributionSet:
        manifest = installed.manifest
        owner = ContributionOwner(
            plugin_id=installed.plugin_id,
            package_digest=installed.package_digest,
            generation_id=generation.generation_id,
        )
        source: PluginSource = "third_party"
        tools = (
            *self._prepare_tools(manifest, owner, source, contributions.tools),
            *self._prepare_discovered_tools(manifest, owner, discovered_tools),
        )
        commands = self._prepare_commands(
            manifest,
            owner,
            source,
            contributions.commands,
        )
        hooks = self._prepare_hooks(manifest, owner, source, contributions.hooks)
        context_hooks = self._prepare_context_providers(
            manifest,
            owner,
            source,
            contributions.context_providers,
        )
        compaction = self._prepare_compaction(
            manifest,
            contributions.compaction_strategies,
        )
        self._validate_background_services(manifest, contributions.background_services)
        continuation_policies = self._prepare_continuation_policies(
            manifest,
            owner,
            contributions.continuation_policies,
        )
        tool_policies = self._prepare_tool_policies(
            manifest,
            owner,
            contributions.tool_policies,
        )
        hooks = (*hooks, *context_hooks, *static_hooks)
        hashes = _contract_hashes(
            manifest,
            tools=tools,
            hooks=hooks,
            commands=commands,
            compaction=compaction,
            continuation_policies=continuation_policies,
            tool_policies=tool_policies,
        )
        return PreparedContributionSet(
            owner=owner,
            tools=tools,
            hooks=hooks,
            commands=commands,
            compaction_strategies=compaction,
            background_services=tuple(contributions.background_services),
            continuation_policies=continuation_policies,
            tool_policies=tool_policies,
            contract_hashes=hashes,
        )

    def commit(
        self,
        prepared: PreparedContributionSet,
        *,
        replace_owner: ContributionOwner | None = None,
    ) -> ContributionLease:
        owner = prepared.owner.key
        with self._lock:
            owners_snapshot = self._owners.copy()
            snapshots = (
                self.tools.snapshot(),
                self.hooks.snapshot(),
                self.commands.snapshot() if self.commands is not None else None,
                self.compaction.snapshot() if self.compaction is not None else None,
                self.continuations.snapshot() if self.continuations is not None else None,
                self.tool_policies.snapshot() if self.tool_policies is not None else None,
            )
            try:
                if replace_owner is not None:
                    previous = replace_owner.key
                    self.tools.unregister_owner(previous)
                    self.hooks.unregister_owner(previous)
                    if self.commands is not None:
                        self.commands.unregister_owner(previous)
                    if self.compaction is not None:
                        self.compaction.unregister_owner(previous)
                    if self.continuations is not None:
                        self.continuations.unregister_owner(previous)
                    if self.tool_policies is not None:
                        self.tool_policies.unregister_owner(previous)
                    self._owners.discard(previous)
                self.tools.replace_owner(owner, prepared.tools)
                self.hooks.replace_owner(owner, prepared.hooks)
                if prepared.commands:
                    if self.commands is None:
                        raise ContributionValidationError("plugin command registry is unavailable")
                    self.commands.replace_owner(owner, prepared.commands)
                if prepared.compaction_strategies:
                    if self.compaction is None:
                        raise ContributionValidationError(
                            "plugin compaction registry is unavailable"
                        )
                    self.compaction.replace_owner(owner, prepared.compaction_strategies)
                if prepared.continuation_policies:
                    if self.continuations is None:
                        raise ContributionValidationError(
                            "run continuation registry is unavailable"
                        )
                    self.continuations.replace_owner(owner, prepared.continuation_policies)
                if prepared.tool_policies:
                    if self.tool_policies is None:
                        raise ContributionValidationError("tool policy registry is unavailable")
                    self.tool_policies.replace_owner(owner, prepared.tool_policies)
                self._owners.add(owner)
            except Exception:
                self.tools.restore(snapshots[0])
                self.hooks.restore(snapshots[1])
                if self.commands is not None and snapshots[2] is not None:
                    self.commands.restore(snapshots[2])
                if self.compaction is not None and snapshots[3] is not None:
                    self.compaction.restore(snapshots[3])
                if self.continuations is not None and snapshots[4] is not None:
                    self.continuations.restore(snapshots[4])
                if self.tool_policies is not None and snapshots[5] is not None:
                    self.tool_policies.restore(snapshots[5])
                self._owners = owners_snapshot
                raise
        return ContributionLease(self, prepared.owner)

    def unregister(self, owner: ContributionOwner) -> None:
        key = owner.key
        with self._lock:
            if key not in self._owners:
                return
            self.tools.unregister_owner(key)
            self.hooks.unregister_owner(key)
            if self.commands is not None:
                self.commands.unregister_owner(key)
            if self.compaction is not None:
                self.compaction.unregister_owner(key)
            if self.continuations is not None:
                self.continuations.unregister_owner(key)
            if self.tool_policies is not None:
                self.tool_policies.unregister_owner(key)
            self._owners.discard(key)

    @staticmethod
    def _prepare_tools(
        manifest: PluginManifest,
        owner: ContributionOwner,
        source: PluginSource,
        tools: list[Tool],
    ) -> tuple[Tool, ...]:
        declarations = {item.exposed_name: item for item in manifest.contributes.tools}
        actual = _unique_contribution_map(
            "tool",
            ((tool.definition.name, tool) for tool in tools),
        )
        _validate_declared_set("tool", declarations, actual)
        prepared: list[Tool] = []
        for name, tool in actual.items():
            declaration = declarations[name]
            sdk_identity = tool.definition.metadata.get("sdk_contribution_id")
            if sdk_identity is not None and sdk_identity != declaration.id:
                raise ContributionValidationError(
                    "tool contribution id does not match manifest: "
                    f"{sdk_identity} != {declaration.id}"
                )
            permission = tool.definition.permission_mode
            if source != "official" and permission == "auto":
                permission = "approval"
            definition = tool.definition.model_copy(
                update={
                    "name": declaration.exposed_name,
                    "description": declaration.description,
                    "source": ("official_plugin" if source == "official" else "third_party_plugin"),
                    "permission_mode": permission,
                }
            )
            contract_hash = _hash(definition.model_dump(mode="json", exclude={"metadata"}))
            definition = definition.model_copy(
                update={
                    "metadata": {
                        **tool.definition.metadata,
                        "plugin_id": manifest.plugin_id,
                        "plugin_version": manifest.version,
                        "package_digest": owner.package_digest,
                        "generation_id": owner.generation_id,
                        "contribution_id": declaration.id,
                        "contract_hash": contract_hash,
                    }
                }
            )
            prepared.append(Tool(definition=definition, handler=tool.handler))
        return tuple(prepared)

    @staticmethod
    def _prepare_commands(
        manifest: PluginManifest,
        owner: ContributionOwner,
        source: PluginSource,
        commands: list[PluginCommand],
    ) -> tuple[PluginCommand, ...]:
        declarations = {item.id: item for item in manifest.contributes.commands}
        actual = _unique_contribution_map(
            "command",
            ((command.name, command) for command in commands),
        )
        _validate_declared_set("command", declarations, actual)
        return tuple(
            replace(
                bind_plugin_command(
                    command,
                    plugin_id=manifest.plugin_id,
                    source=source,
                    maximum_timeout_seconds=5.0,
                ),
                generation_id=owner.generation_id,
                allow_session_inputs=_allows_session_inputs(manifest),
            )
            for command in actual.values()
        )

    @staticmethod
    def _prepare_discovered_tools(
        manifest: PluginManifest,
        owner: ContributionOwner,
        tools: tuple[Tool, ...],
    ) -> tuple[Tool, ...]:
        prepared: list[Tool] = []
        for tool in tools:
            remote_name = str(tool.definition.metadata.get("mcp_tool_name", ""))
            server_id = str(tool.definition.metadata.get("mcp_server_id", ""))
            if not remote_name or not server_id:
                raise ContributionValidationError(
                    "dynamically discovered tools must carry MCP identity metadata"
                )
            contract_hash = _hash(tool.definition.model_dump(mode="json", exclude={"metadata"}))
            definition = tool.definition.model_copy(
                update={
                    "permission_mode": "approval",
                    "metadata": {
                        **tool.definition.metadata,
                        "plugin_id": manifest.plugin_id,
                        "plugin_version": manifest.version,
                        "package_digest": owner.package_digest,
                        "generation_id": owner.generation_id,
                        "contribution_id": f"mcp:{server_id}:{remote_name}",
                        "contract_hash": contract_hash,
                    },
                }
            )
            prepared.append(Tool(definition=definition, handler=tool.handler))
        return tuple(prepared)

    @staticmethod
    def _prepare_hooks(
        manifest: PluginManifest,
        owner: ContributionOwner,
        source: PluginSource,
        hooks: list[HookRegistration],
    ) -> tuple[HookRegistration, ...]:
        declarations = {item.id: item for item in manifest.contributes.hooks}
        actual: dict[str, HookRegistration] = {}
        for hook in hooks:
            hook_id = hook.hook_id
            if not hook_id:
                matches = [
                    item.id
                    for item in manifest.contributes.hooks
                    if item.event == hook.hook_name and item.id not in actual
                ]
                if len(matches) != 1:
                    raise ContributionValidationError(
                        f"hook contribution requires stable hook_id: {hook.hook_name}"
                    )
                hook_id = matches[0]
            if hook_id in actual:
                raise ContributionValidationError(f"duplicate hook contribution: {hook_id}")
            actual[hook_id] = hook
        _validate_declared_set("hook", declarations, actual)
        prepared: list[HookRegistration] = []
        for hook_id, hook in actual.items():
            declaration = declarations[hook_id]
            if hook.hook_name != declaration.event:
                raise ContributionValidationError(f"hook event does not match manifest: {hook_id}")
            maximum = 1.0 if hook.hook_name == "on_event" else 10.0
            timeout = declaration.timeout_seconds or hook.timeout_seconds or maximum
            prepared.append(
                replace(
                    hook,
                    plugin_id=manifest.plugin_id,
                    source=source,
                    priority=declaration.priority,
                    timeout_seconds=min(timeout, maximum),
                    hook_id=hook_id,
                    owner=owner.key,
                    generation_id=owner.generation_id,
                )
            )
        return tuple(prepared)

    @staticmethod
    def _prepare_context_providers(
        manifest: PluginManifest,
        owner: ContributionOwner,
        source: PluginSource,
        providers: list[PluginContextProvider],
    ) -> tuple[HookRegistration, ...]:
        declarations = {item.id: item for item in manifest.contributes.context_providers}
        actual = _unique_contribution_map(
            "context provider",
            ((provider.provider_id, provider) for provider in providers),
        )
        _validate_declared_set("context provider", declarations, actual)
        return tuple(
            HookRegistration(
                plugin_id=manifest.plugin_id,
                hook_name="after_context_build",
                handler=context_provider_hook(provider),
                source=source,
                priority=provider.priority,
                timeout_seconds=10.0,
                hook_id=f"context:{provider.provider_id}",
                owner=owner.key,
                generation_id=owner.generation_id,
            )
            for provider in actual.values()
        )

    @staticmethod
    def _prepare_compaction(
        manifest: PluginManifest,
        strategies: list[CompactionStrategy],
    ) -> tuple[CompactionStrategy, ...]:
        declarations = {item.id: item for item in manifest.contributes.compaction_strategies}
        actual: dict[str, CompactionStrategy] = {}
        for strategy in strategies:
            if strategy.strategy_id.startswith("builtin."):
                raise ContributionValidationError(
                    f"plugin compaction strategy cannot use reserved id: {strategy.strategy_id}"
                )
            local_id = strategy.strategy_id
            prefix = f"{manifest.plugin_id}."
            if local_id.startswith(prefix):
                local_id = local_id[len(prefix) :]
            if local_id in actual:
                raise ContributionValidationError(
                    f"duplicate compaction strategy contribution: {local_id}"
                )
            actual[local_id] = strategy
        _validate_declared_set("compaction strategy", declarations, actual)
        return tuple(
            _BoundCompactionStrategy(
                strategy_id=f"{manifest.plugin_id}.{local_id}",
                version=strategy.version,
                delegate=strategy,
            )
            for local_id, strategy in actual.items()
        )

    @staticmethod
    def _validate_background_services(
        manifest: PluginManifest,
        services: list[BackgroundService],
    ) -> None:
        declarations = {item.id: item for item in manifest.contributes.background_services}
        actual = _unique_contribution_map(
            "background service",
            ((service.service_id, service) for service in services),
        )
        _validate_declared_set("background service", declarations, actual)

    @staticmethod
    def _prepare_continuation_policies(
        manifest: PluginManifest,
        owner: ContributionOwner,
        policies: list[ContinuationPolicyRegistration],
    ) -> tuple[ContinuationPolicyRegistration, ...]:
        declarations = {item.id: item for item in manifest.contributes.continuation_policies}
        actual = _unique_contribution_map(
            "continuation policy",
            ((policy.policy_id, policy) for policy in policies),
        )
        _validate_declared_set("continuation policy", declarations, actual)
        if actual and not _allows_session_inputs(manifest):
            raise ContributionValidationError(
                "continuation policies require sessions.write and background.run permissions"
            )
        return tuple(
            replace(
                policy,
                plugin_id=manifest.plugin_id,
                priority=declarations[policy.policy_id].priority,
                timeout_seconds=declarations[policy.policy_id].timeout_seconds,
                owner=owner.key,
                generation_id=owner.generation_id,
            )
            for policy in actual.values()
        )

    @staticmethod
    def _prepare_tool_policies(
        manifest: PluginManifest,
        owner: ContributionOwner,
        policies: list[ToolPolicyRegistration],
    ) -> tuple[ToolPolicyRegistration, ...]:
        declarations = {item.id: item for item in manifest.contributes.tool_policies}
        actual = _unique_contribution_map(
            "tool policy",
            ((policy.policy_id, policy) for policy in policies),
        )
        _validate_declared_set("tool policy", declarations, actual)
        return tuple(
            replace(
                policy,
                plugin_id=manifest.plugin_id,
                priority=declarations[policy.policy_id].priority,
                timeout_seconds=declarations[policy.policy_id].timeout_seconds,
                owner=owner.key,
                generation_id=owner.generation_id,
            )
            for policy in actual.values()
        )


def _unique_contribution_map[ContributionT](
    kind: str,
    values: Iterable[tuple[str, ContributionT]],
) -> dict[str, ContributionT]:
    indexed: dict[str, ContributionT] = {}
    for identity, value in values:
        if identity in indexed:
            raise ContributionValidationError(f"duplicate {kind} contribution: {identity}")
        indexed[identity] = value
    return indexed


def _allows_session_inputs(manifest: PluginManifest) -> bool:
    permissions = {request.id for request in manifest.permissions}
    return {"sessions.write", "background.run"}.issubset(permissions)


def _validate_declared_set(
    kind: str,
    declarations: Mapping[str, object],
    actual: Mapping[str, object],
) -> None:
    extra = sorted(set(actual) - set(declarations))
    missing = sorted(
        identity
        for identity, declaration in declarations.items()
        if identity not in actual and not bool(getattr(declaration, "optional", False))
    )
    if extra:
        raise ContributionValidationError(
            f"{kind} contributions are not declared in manifest: {', '.join(extra)}"
        )
    if missing:
        raise ContributionValidationError(
            f"required {kind} contributions are missing: {', '.join(missing)}"
        )


def _contract_hashes(
    manifest: PluginManifest,
    *,
    tools: tuple[Tool, ...],
    hooks: tuple[HookRegistration, ...],
    commands: tuple[PluginCommand, ...],
    compaction: tuple[CompactionStrategy, ...],
    continuation_policies: tuple[ContinuationPolicyRegistration, ...],
    tool_policies: tuple[ToolPolicyRegistration, ...],
) -> dict[str, str]:
    contracts: dict[str, object] = {}
    for tool in tools:
        local_id = str(tool.definition.metadata["contribution_id"])
        contracts[f"{manifest.plugin_id}/tools/{local_id}"] = tool.definition.model_dump(
            mode="json",
            exclude={"metadata"},
        )
    for hook in hooks:
        if hook.hook_id.startswith("skill:"):
            continue
        if hook.hook_id.startswith("context:"):
            kind = "context_providers"
            local_id = hook.hook_id.removeprefix("context:")
        else:
            kind = "hooks"
            local_id = hook.hook_id
        contracts[f"{manifest.plugin_id}/{kind}/{local_id}"] = {
            "event": hook.hook_name,
            "priority": hook.priority,
        }
    for command in commands:
        contracts[f"{manifest.plugin_id}/commands/{command.name}"] = {
            "description": command.description,
        }
    for strategy in compaction:
        local_id = strategy.strategy_id.removeprefix(f"{manifest.plugin_id}.")
        contracts[f"{manifest.plugin_id}/compaction_strategies/{local_id}"] = {
            "version": strategy.version,
        }
    for continuation_policy in continuation_policies:
        contracts[f"{manifest.plugin_id}/continuation_policies/{continuation_policy.policy_id}"] = {
            "priority": continuation_policy.priority,
            "timeout_seconds": continuation_policy.timeout_seconds,
        }
    for tool_policy in tool_policies:
        contracts[f"{manifest.plugin_id}/tool_policies/{tool_policy.policy_id}"] = {
            "priority": tool_policy.priority,
            "timeout_seconds": tool_policy.timeout_seconds,
            "restriction_only": True,
        }
    for skill in manifest.contributes.skills:
        contracts[f"{manifest.plugin_id}/skills/{skill.id}"] = {"path": skill.path}
    for server in manifest.contributes.mcp_servers:
        contracts[f"{manifest.plugin_id}/mcp_servers/{server.id}"] = {
            "path": server.path,
            "auto_start": server.auto_start,
        }
    return {identity: _hash(value) for identity, value in contracts.items()}


def _hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
