from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from ouro.core.cancellation import AbortSignal
from ouro.core.compaction import (
    CompactionArtifact,
    CompactionRequest,
    CompactionStrategy,
    CompactionStrategyRegistry,
)
from ouro.core.context_providers import (
    ContextProviderRegistration,
    ContextProviderRegistry,
)
from ouro.core.continuations import (
    ContinuationPolicyRegistration,
    RunContinuationRegistry,
)
from ouro.core.hooks import HookRegistration, HookRegistry, PluginSource
from ouro.core.tool_policies import ToolPolicyRegistration, ToolPolicyRegistry
from ouro.core.turn_policies import TurnPolicyRegistration, TurnPolicyRegistry
from ouro.plugins.candidates import PluginRuntimeCandidate
from ouro.plugins.contracts import (
    ContributionContractContext,
    build_contribution_contract_hashes,
)
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.models import PluginClass, PluginGenerationRecord
from ouro.plugins.registry import (
    PluginCommand,
    PluginCommandRegistry,
    bind_plugin_command,
)
from ouro.skills import SkillMetadata
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
    context_providers: list[ContextProviderRegistration] = field(default_factory=list)
    compaction_strategies: list[CompactionStrategy] = field(default_factory=list)
    background_services: list[BackgroundService] = field(default_factory=list)
    continuation_policies: list[ContinuationPolicyRegistration] = field(default_factory=list)
    turn_policies: list[TurnPolicyRegistration] = field(default_factory=list)
    tool_policies: list[ToolPolicyRegistration] = field(default_factory=list)


@dataclass(frozen=True)
class ContributionOwner:
    plugin_id: str
    plugin_class: PluginClass
    release_id: str
    generation_id: str
    package_digest: str | None = None
    builtin_contract_hash: str | None = None

    @property
    def key(self) -> str:
        return f"{self.plugin_id}@{self.release_id}#{self.generation_id}"


@dataclass(frozen=True)
class PreparedContributionSet:
    owner: ContributionOwner
    tools: tuple[Tool, ...] = ()
    hooks: tuple[HookRegistration, ...] = ()
    context_providers: tuple[ContextProviderRegistration, ...] = ()
    commands: tuple[PluginCommand, ...] = ()
    compaction_strategies: tuple[CompactionStrategy, ...] = ()
    background_services: tuple[BackgroundService, ...] = ()
    skills: tuple[SkillMetadata, ...] = ()
    continuation_policies: tuple[ContinuationPolicyRegistration, ...] = ()
    turn_policies: tuple[TurnPolicyRegistration, ...] = ()
    tool_policies: tuple[ToolPolicyRegistration, ...] = ()
    contract_hashes: dict[str, str] = field(default_factory=dict)
    degraded_reasons: tuple[str, ...] = ()


class ContributionValidationError(ValueError):
    pass


def bind_execution_contract(
    prepared: PreparedContributionSet,
    execution_contract_hash: str,
) -> PreparedContributionSet:
    """Attach one stable generation contract to every durable contribution surface."""

    if len(execution_contract_hash) != 64:
        raise ValueError("execution contract hash must be a SHA-256 digest")
    plugin_id = prepared.owner.plugin_id
    tools = tuple(
        Tool(
            definition=tool.definition.model_copy(
                update={
                    "metadata": {
                        **tool.definition.metadata,
                        "execution_contract_hash": execution_contract_hash,
                    }
                }
            ),
            handler=tool.handler,
            resume_handler=tool.resume_handler,
        )
        for tool in prepared.tools
    )
    commands = tuple(
        replace(command, execution_contract_hash=execution_contract_hash)
        for command in prepared.commands
    )
    continuation_policies = tuple(
        replace(
            policy,
            execution_contract_hash=execution_contract_hash,
            contract_hash=prepared.contract_hashes[
                f"{plugin_id}/continuation_policies/{policy.policy_id}"
            ],
        )
        for policy in prepared.continuation_policies
    )
    skills = tuple(
        skill.model_copy(
            update={
                "extensions": {
                    **skill.extensions,
                    "ouro": {
                        **dict(skill.extensions.get("ouro", {})),
                        "execution_contract_hash": execution_contract_hash,
                    },
                }
            }
        )
        for skill in prepared.skills
    )
    return replace(
        prepared,
        tools=tools,
        commands=commands,
        continuation_policies=continuation_policies,
        skills=skills,
    )


@dataclass(frozen=True)
class _PreparationContext:
    candidate: PluginRuntimeCandidate
    generation: PluginGenerationRecord
    contributions: PluginContributions
    owner: ContributionOwner
    source: PluginSource
    resource_skills: tuple[SkillMetadata, ...]
    discovered_tools: tuple[Tool, ...]


@dataclass
class _PreparedBuilder:
    tools: tuple[Tool, ...] = ()
    hooks: tuple[HookRegistration, ...] = ()
    context_providers: tuple[ContextProviderRegistration, ...] = ()
    commands: tuple[PluginCommand, ...] = ()
    compaction_strategies: tuple[CompactionStrategy, ...] = ()
    background_services: tuple[BackgroundService, ...] = ()
    skills: tuple[SkillMetadata, ...] = ()
    continuation_policies: tuple[ContinuationPolicyRegistration, ...] = ()
    turn_policies: tuple[TurnPolicyRegistration, ...] = ()
    tool_policies: tuple[ToolPolicyRegistration, ...] = ()


class ContributionPreparationAdapter(Protocol):
    @property
    def kind(self) -> str: ...

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None: ...

    def missing_optional(
        self,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> tuple[str, ...]: ...


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


class ContributionCoordinator:
    """Validate one generation against stable Host contribution contracts."""

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        hooks: HookRegistry,
        context_providers: ContextProviderRegistry | None = None,
        commands: PluginCommandRegistry | None = None,
        compaction: CompactionStrategyRegistry | None = None,
        continuations: RunContinuationRegistry | None = None,
        turn_policies: TurnPolicyRegistry | None = None,
        tool_policies: ToolPolicyRegistry | None = None,
    ) -> None:
        self.tools = tools
        self.hooks = hooks
        self.context_providers = context_providers or ContextProviderRegistry()
        self.commands = commands
        self.compaction = compaction
        self.continuations = continuations
        self.turn_policies = turn_policies
        self.tool_policies = tool_policies

    def prepare(
        self,
        candidate: PluginRuntimeCandidate,
        generation: PluginGenerationRecord,
        contributions: PluginContributions,
        *,
        resource_skills: tuple[SkillMetadata, ...] = (),
        discovered_tools: tuple[Tool, ...] = (),
        resource_contracts: Mapping[str, object] | None = None,
    ) -> PreparedContributionSet:
        manifest = candidate.manifest
        owner = ContributionOwner(
            plugin_id=candidate.plugin_id,
            plugin_class=candidate.plugin_class,
            release_id=candidate.release_id,
            generation_id=generation.generation_id,
            package_digest=candidate.package_digest,
            builtin_contract_hash=candidate.builtin_contract_hash,
        )
        source: PluginSource = "official" if candidate.plugin_class == "builtin" else "third_party"
        context = _PreparationContext(
            candidate=candidate,
            generation=generation,
            contributions=contributions,
            owner=owner,
            source=source,
            resource_skills=resource_skills,
            discovered_tools=discovered_tools,
        )
        from ouro.plugins.contribution_types import contribution_type_adapters

        adapters = contribution_type_adapters()
        builder = _PreparedBuilder()
        for adapter in adapters:
            if adapter.preparation is not None:
                adapter.preparation.prepare(self, context, builder)
        hashes = build_contribution_contract_hashes(
            ContributionContractContext(
                manifest=manifest,
                tools=builder.tools,
                hooks=builder.hooks,
                context_providers=builder.context_providers,
                commands=builder.commands,
                compaction=builder.compaction_strategies,
                background_services=builder.background_services,
                continuation_policies=builder.continuation_policies,
                turn_policies=builder.turn_policies,
                tool_policies=builder.tool_policies,
                resource_contracts=dict(resource_contracts or {}),
            )
        )
        degraded_reasons = tuple(
            sorted(
                reason
                for adapter in adapters
                if adapter.preparation is not None
                for reason in adapter.preparation.missing_optional(context, builder)
            )
        )
        return PreparedContributionSet(
            owner=owner,
            tools=builder.tools,
            hooks=builder.hooks,
            context_providers=builder.context_providers,
            commands=builder.commands,
            compaction_strategies=builder.compaction_strategies,
            background_services=builder.background_services,
            skills=builder.skills,
            continuation_policies=builder.continuation_policies,
            turn_policies=builder.turn_policies,
            tool_policies=builder.tool_policies,
            contract_hashes=hashes,
            degraded_reasons=degraded_reasons,
        )

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
            if sdk_identity is not None and (
                tool.definition.permission_mode != declaration.permission_mode
                or tool.definition.permission_control != declaration.permission_control
                or tool.definition.effect != declaration.effect
                or tool.definition.batch_behavior != declaration.batch_behavior
                or tool.definition.resumable != declaration.resumable
            ):
                raise ContributionValidationError(
                    f"tool permission contract does not match manifest: {declaration.exposed_name}"
                )
            definition = tool.definition.model_copy(
                update={
                    "name": declaration.exposed_name,
                    "description": declaration.description,
                    "source": ("official_plugin" if source == "official" else "third_party_plugin"),
                    "permission_mode": declaration.permission_mode,
                    "permission_control": declaration.permission_control,
                    "effect": declaration.effect,
                    "batch_behavior": declaration.batch_behavior,
                    "resumable": declaration.resumable,
                }
            )
            contract_hash = _hash(definition.model_dump(mode="json", exclude={"metadata"}))
            definition = definition.model_copy(
                update={
                    "metadata": {
                        **tool.definition.metadata,
                        "plugin_id": manifest.plugin_id,
                        "plugin_version": manifest.version,
                        "plugin_class": owner.plugin_class,
                        "release_id": owner.release_id,
                        "package_digest": owner.package_digest,
                        "builtin_contract_hash": owner.builtin_contract_hash,
                        "generation_id": owner.generation_id,
                        "contribution_id": declaration.id,
                        "contract_hash": contract_hash,
                    }
                }
            )
            prepared.append(
                Tool(
                    definition=definition,
                    handler=tool.handler,
                    resume_handler=tool.resume_handler,
                )
            )
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
                        "plugin_class": owner.plugin_class,
                        "release_id": owner.release_id,
                        "package_digest": owner.package_digest,
                        "builtin_contract_hash": owner.builtin_contract_hash,
                        "generation_id": owner.generation_id,
                        "contribution_id": f"mcp:{server_id}:{remote_name}",
                        "contract_hash": contract_hash,
                    },
                }
            )
            prepared.append(
                Tool(
                    definition=definition,
                    handler=tool.handler,
                    resume_handler=tool.resume_handler,
                )
            )
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
        providers: list[ContextProviderRegistration],
    ) -> tuple[ContextProviderRegistration, ...]:
        declarations = {item.id: item for item in manifest.contributes.context_providers}
        actual = _unique_contribution_map(
            "context provider",
            ((provider.provider_id, provider) for provider in providers),
        )
        _validate_declared_set("context provider", declarations, actual)
        return tuple(
            replace(
                provider,
                plugin_id=manifest.plugin_id,
                priority=declarations[provider.provider_id].priority,
                timeout_seconds=declarations[provider.provider_id].timeout_seconds,
                failure_mode=declarations[provider.provider_id].failure_mode,
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

    @staticmethod
    def _prepare_turn_policies(
        manifest: PluginManifest,
        owner: ContributionOwner,
        policies: list[TurnPolicyRegistration],
    ) -> tuple[TurnPolicyRegistration, ...]:
        declarations = {item.id: item for item in manifest.contributes.turn_policies}
        actual = _unique_contribution_map(
            "turn policy",
            ((policy.policy_id, policy) for policy in policies),
        )
        _validate_declared_set("turn policy", declarations, actual)
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


@dataclass(frozen=True)
class _ToolPreparationAdapter:
    kind: str = "tools"

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None:
        builder.tools = coordinator._prepare_tools(
            context.candidate.manifest,
            context.owner,
            context.source,
            context.contributions.tools,
        )

    def missing_optional(
        self,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> tuple[str, ...]:
        del builder
        return _missing_optional(
            "tools",
            context.candidate.manifest.contributes.tools,
            {tool.definition.name for tool in context.contributions.tools},
            exposed_name=True,
        )


@dataclass(frozen=True)
class _HookPreparationAdapter:
    kind: str = "hooks"

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None:
        builder.hooks = (
            *builder.hooks,
            *coordinator._prepare_hooks(
                context.candidate.manifest,
                context.owner,
                context.source,
                context.contributions.hooks,
            ),
        )

    def missing_optional(
        self,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> tuple[str, ...]:
        return _missing_optional(
            "hooks",
            context.candidate.manifest.contributes.hooks,
            {hook.hook_id for hook in builder.hooks if not hook.hook_id.startswith("skill:")},
        )


@dataclass(frozen=True)
class _ContextPreparationAdapter:
    kind: str = "context_providers"

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None:
        builder.context_providers = coordinator._prepare_context_providers(
            context.candidate.manifest,
            context.owner,
            context.contributions.context_providers,
        )

    def missing_optional(
        self,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> tuple[str, ...]:
        return _missing_optional(
            "context_providers",
            context.candidate.manifest.contributes.context_providers,
            {provider.provider_id for provider in builder.context_providers},
        )


@dataclass(frozen=True)
class _CommandPreparationAdapter:
    kind: str = "commands"

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None:
        builder.commands = coordinator._prepare_commands(
            context.candidate.manifest,
            context.owner,
            context.source,
            context.contributions.commands,
        )

    def missing_optional(
        self,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> tuple[str, ...]:
        return _missing_optional(
            "commands",
            context.candidate.manifest.contributes.commands,
            {command.name for command in builder.commands},
        )


@dataclass(frozen=True)
class _CompactionPreparationAdapter:
    kind: str = "compaction_strategies"

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None:
        builder.compaction_strategies = coordinator._prepare_compaction(
            context.candidate.manifest,
            context.contributions.compaction_strategies,
        )

    def missing_optional(
        self,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> tuple[str, ...]:
        return _missing_optional(
            "compaction_strategies",
            context.candidate.manifest.contributes.compaction_strategies,
            {
                strategy.strategy_id.removeprefix(f"{context.candidate.plugin_id}.")
                for strategy in builder.compaction_strategies
            },
        )


@dataclass(frozen=True)
class _SkillPreparationAdapter:
    kind: str = "skills"

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None:
        del coordinator
        builder.skills = context.resource_skills

    def missing_optional(
        self,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> tuple[str, ...]:
        return _missing_optional(
            "skills",
            context.candidate.manifest.contributes.skills,
            {skill.name for skill in builder.skills},
        )


@dataclass(frozen=True)
class _MCPPreparationAdapter:
    kind: str = "mcp_servers"

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None:
        builder.tools = (
            *builder.tools,
            *coordinator._prepare_discovered_tools(
                context.candidate.manifest,
                context.owner,
                context.discovered_tools,
            ),
        )

    def missing_optional(
        self,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> tuple[str, ...]:
        del context, builder
        return ()


@dataclass(frozen=True)
class _BackgroundServicePreparationAdapter:
    kind: str = "background_services"

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None:
        coordinator._validate_background_services(
            context.candidate.manifest,
            context.contributions.background_services,
        )
        builder.background_services = tuple(context.contributions.background_services)

    def missing_optional(
        self,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> tuple[str, ...]:
        return _missing_optional(
            "background_services",
            context.candidate.manifest.contributes.background_services,
            {service.service_id for service in builder.background_services},
        )


@dataclass(frozen=True)
class _ContinuationPreparationAdapter:
    kind: str = "continuation_policies"

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None:
        builder.continuation_policies = coordinator._prepare_continuation_policies(
            context.candidate.manifest,
            context.owner,
            context.contributions.continuation_policies,
        )

    def missing_optional(
        self,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> tuple[str, ...]:
        return _missing_optional(
            "continuation_policies",
            context.candidate.manifest.contributes.continuation_policies,
            {policy.policy_id for policy in builder.continuation_policies},
        )


@dataclass(frozen=True)
class _ToolPolicyPreparationAdapter:
    kind: str = "tool_policies"

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None:
        builder.tool_policies = coordinator._prepare_tool_policies(
            context.candidate.manifest,
            context.owner,
            context.contributions.tool_policies,
        )

    def missing_optional(
        self,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> tuple[str, ...]:
        return _missing_optional(
            "tool_policies",
            context.candidate.manifest.contributes.tool_policies,
            {policy.policy_id for policy in builder.tool_policies},
        )


@dataclass(frozen=True)
class _TurnPolicyPreparationAdapter:
    kind: str = "turn_policies"

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None:
        builder.turn_policies = coordinator._prepare_turn_policies(
            context.candidate.manifest,
            context.owner,
            context.contributions.turn_policies,
        )

    def missing_optional(
        self,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> tuple[str, ...]:
        return _missing_optional(
            "turn_policies",
            context.candidate.manifest.contributes.turn_policies,
            {policy.policy_id for policy in builder.turn_policies},
        )


TOOL_PREPARATION_ADAPTER = _ToolPreparationAdapter()
HOOK_PREPARATION_ADAPTER = _HookPreparationAdapter()
COMMAND_PREPARATION_ADAPTER = _CommandPreparationAdapter()
CONTEXT_PREPARATION_ADAPTER = _ContextPreparationAdapter()
COMPACTION_PREPARATION_ADAPTER = _CompactionPreparationAdapter()
SKILL_PREPARATION_ADAPTER = _SkillPreparationAdapter()
MCP_PREPARATION_ADAPTER = _MCPPreparationAdapter()
BACKGROUND_SERVICE_PREPARATION_ADAPTER = _BackgroundServicePreparationAdapter()
CONTINUATION_PREPARATION_ADAPTER = _ContinuationPreparationAdapter()
TOOL_POLICY_PREPARATION_ADAPTER = _ToolPolicyPreparationAdapter()
TURN_POLICY_PREPARATION_ADAPTER = _TurnPolicyPreparationAdapter()


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


def _missing_optional(
    kind: str,
    declarations: Iterable[Any],
    actual: set[str],
    *,
    exposed_name: bool = False,
) -> tuple[str, ...]:
    return tuple(
        f"{kind}/{declaration.id}"
        for declaration in declarations
        if declaration.optional
        and str(
            getattr(
                declaration,
                "exposed_name" if exposed_name else "id",
            )
        )
        not in actual
    )


def _hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
