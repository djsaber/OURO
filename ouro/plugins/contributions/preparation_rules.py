"""Validate and bind contribution records without owning lifecycle or catalogs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from ouro.core.cancellation import AbortSignal
from ouro.core.compaction import CompactionArtifact, CompactionRequest, CompactionStrategy
from ouro.core.context_providers import ContextProviderRegistration
from ouro.core.continuations import ContinuationPolicyRegistration
from ouro.core.extensions import PluginSource
from ouro.core.observers import ObserverRegistration
from ouro.core.prompt_policies import PromptPolicyRegistration
from ouro.core.tool_policies import ToolPolicyRegistration
from ouro.core.turn_policies import TurnPolicyRegistration
from ouro.plugins.contributions.models import (
    BackgroundService,
    ContributionOwner,
    ContributionValidationError,
    PluginContributions,
    PreparedBackgroundService,
)
from ouro.plugins.manifest import PluginManifest, ToolContributionSpec
from ouro.plugins.providers import ProviderRegistration
from ouro.plugins.registry import PluginCommand, PluginCommandArgument, bind_plugin_command
from ouro.plugins.run_drivers import RunDriverRegistration
from ouro.plugins.session_controllers import PluginSessionController, bind_plugin_session_controller
from ouro.skills.models import SkillMetadata
from ouro.tools.base import Tool


@dataclass(frozen=True)
class _PreparationContext:
    manifest: PluginManifest
    contributions: PluginContributions
    owner: ContributionOwner
    source: PluginSource
    resource_skills: tuple[SkillMetadata, ...]
    discovered_tools: tuple[Tool, ...]


@dataclass
class _PreparedBuilder:
    tools: tuple[Tool, ...] = ()
    observers: tuple[ObserverRegistration, ...] = ()
    context_providers: tuple[ContextProviderRegistration, ...] = ()
    commands: tuple[PluginCommand, ...] = ()
    compaction_strategies: tuple[CompactionStrategy, ...] = ()
    background_services: tuple[PreparedBackgroundService, ...] = ()
    skills: tuple[SkillMetadata, ...] = ()
    providers: tuple[ProviderRegistration, ...] = ()
    run_drivers: tuple[RunDriverRegistration, ...] = ()
    continuation_policies: tuple[ContinuationPolicyRegistration, ...] = ()
    prompt_policies: tuple[PromptPolicyRegistration, ...] = ()
    turn_policies: tuple[TurnPolicyRegistration, ...] = ()
    tool_policies: tuple[ToolPolicyRegistration, ...] = ()
    session_controllers: tuple[PluginSessionController, ...] = ()


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


def _canonical_controller_id(plugin_id: str, controller_id: str | None) -> str | None:
    if controller_id is None:
        return None
    return f"{plugin_id}/session_controllers/{controller_id}"


def _hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def prepare_tools(
    manifest: PluginManifest,
    owner: ContributionOwner,
    source: PluginSource,
    tools: list[Tool],
    *,
    declarations: list[ToolContributionSpec] | None = None,
) -> tuple[Tool, ...]:
    declared = {
        item.exposed_name: item
        for item in (manifest.contributes.tools if declarations is None else declarations)
    }
    actual = _unique_contribution_map(
        "tool",
        ((tool.definition.name, tool) for tool in tools),
    )
    _validate_declared_set("tool", declared, actual)
    prepared: list[Tool] = []
    for name, tool in actual.items():
        declaration = declared[name]
        sdk_identity = tool.definition.metadata.get("sdk_contribution_id")
        if sdk_identity is not None and sdk_identity != declaration.id:
            raise ContributionValidationError(
                f"tool contribution id does not match manifest: {sdk_identity} != {declaration.id}"
            )
        if sdk_identity is not None and tool.definition.description != declaration.description:
            raise ContributionValidationError(
                f"tool description does not match manifest: {declaration.exposed_name}"
            )
        if sdk_identity is not None and (
            tool.definition.permission_mode != declaration.permission_mode
            or tool.definition.permission_control != declaration.permission_control
            or tool.definition.effect != declaration.effect
            or tool.definition.batch_behavior != declaration.batch_behavior
            or tool.definition.resumable != declaration.resumable
            or tool.definition.supports_unattended != declaration.supports_unattended
            or tool.definition.active_when_controller_id != declaration.active_when
            or tool.definition.interaction_controller_id != declaration.interaction_controller
        ):
            raise ContributionValidationError(
                f"tool contract does not match manifest: {declaration.exposed_name}"
            )
        definition = tool.definition.model_copy(
            deep=True,
            update={
                "name": declaration.exposed_name,
                "canonical_id": f"{manifest.plugin_id}/tools/{declaration.id}",
                "description": declaration.description,
                "source": ("official_plugin" if source == "official" else "third_party_plugin"),
                "permission_mode": declaration.permission_mode,
                "permission_control": declaration.permission_control,
                "effect": declaration.effect,
                "batch_behavior": declaration.batch_behavior,
                "resumable": declaration.resumable,
                "supports_unattended": declaration.supports_unattended,
                "active_when_controller_id": _canonical_controller_id(
                    manifest.plugin_id,
                    declaration.active_when,
                ),
                "interaction_controller_id": _canonical_controller_id(
                    manifest.plugin_id,
                    declaration.interaction_controller,
                ),
            },
        )
        contract_hash = _hash(definition.execution_contract())
        definition = definition.model_copy(
            update={
                "metadata": {
                    **tool.definition.metadata,
                    "plugin_id": manifest.plugin_id,
                    "plugin_version": manifest.version,
                    "plugin_class": owner.plugin_class,
                    "release_id": owner.release_id,
                    "package_digest": owner.package_digest,
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
            description=declarations[command_id].description,
            usage=declarations[command_id].usage,
            argument_options=tuple(
                PluginCommandArgument(
                    value=item.value,
                    description=item.description,
                    usage=item.usage,
                )
                for item in declarations[command_id].argument_options
            ),
            requires_session=declarations[command_id].requires_session,
        )
        for command_id, command in actual.items()
    )


def _prepare_session_controllers(
    manifest: PluginManifest,
    owner: ContributionOwner,
    source: PluginSource,
    controllers: list[PluginSessionController],
) -> tuple[PluginSessionController, ...]:
    declarations = {item.id: item for item in manifest.contributes.session_controllers}
    actual = _unique_contribution_map(
        "session controller",
        ((controller.controller_id, controller) for controller in controllers),
    )
    _validate_declared_set("session controller", declarations, actual)
    prepared: list[PluginSessionController] = []
    for controller_id, controller in actual.items():
        declaration = declarations[controller_id]
        declared_claims = tuple((claim.domain, claim.access) for claim in declaration.claims)
        actual_claims = tuple((claim.domain, claim.access) for claim in controller.claims)
        if actual_claims != declared_claims:
            raise ContributionValidationError(
                f"session controller claims do not match manifest: {controller_id}"
            )
        prepared.append(
            bind_plugin_session_controller(
                replace(
                    controller,
                    description=declaration.description,
                    timeout_seconds=declaration.timeout_seconds,
                ),
                plugin_id=manifest.plugin_id,
                source=source,
                plugin_class=owner.plugin_class,
                release_id=owner.release_id,
                package_digest=owner.package_digest,
                generation_id=owner.generation_id,
                maximum_timeout_seconds=5.0,
            )
        )
    return tuple(prepared)


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
        definition = tool.definition.model_copy(
            update={
                "canonical_id": f"{manifest.plugin_id}/tools/mcp:{server_id}:{remote_name}",
                "permission_mode": "approval",
            }
        )
        contract_hash = _hash(definition.execution_contract())
        definition = definition.model_copy(
            update={
                "metadata": {
                    **tool.definition.metadata,
                    "plugin_id": manifest.plugin_id,
                    "plugin_version": manifest.version,
                    "plugin_class": owner.plugin_class,
                    "release_id": owner.release_id,
                    "package_digest": owner.package_digest,
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


def _prepare_observers(
    manifest: PluginManifest,
    owner: ContributionOwner,
    source: PluginSource,
    observers: list[ObserverRegistration],
) -> tuple[ObserverRegistration, ...]:
    declarations = {item.id: item for item in manifest.contributes.observers}
    actual = _unique_contribution_map(
        "observer",
        ((observer.observer_id, observer) for observer in observers),
    )
    _validate_declared_set("observer", declarations, actual)
    prepared: list[ObserverRegistration] = []
    for observer_id, observer in actual.items():
        declaration = declarations[observer_id]
        if observer.event != declaration.event:
            raise ContributionValidationError(
                f"observer event does not match manifest: {observer_id}"
            )
        if tuple(observer.event_types) != tuple(declaration.event_types):
            raise ContributionValidationError(
                f"observer event_types do not match manifest: {observer_id}"
            )
        prepared.append(
            replace(
                observer,
                plugin_id=manifest.plugin_id,
                source=source,
                priority=declaration.priority,
                timeout_seconds=declaration.timeout_seconds,
                observer_id=observer_id,
                owner=owner.key,
                generation_id=owner.generation_id,
                active_when_controller_id=_canonical_controller_id(
                    manifest.plugin_id,
                    declaration.active_when,
                ),
            )
        )
    return tuple(prepared)


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
            active_when_controller_id=_canonical_controller_id(
                manifest.plugin_id,
                declarations[provider.provider_id].active_when,
            ),
        )
        for provider in actual.values()
    )


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
            active_when_controller_id=_canonical_controller_id(
                manifest.plugin_id,
                declarations[policy.policy_id].active_when,
            ),
        )
        for policy in actual.values()
    )


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
            active_when_controller_id=_canonical_controller_id(
                manifest.plugin_id,
                declarations[policy.policy_id].active_when,
            ),
        )
        for policy in actual.values()
    )


def _prepare_prompt_policies(
    manifest: PluginManifest,
    owner: ContributionOwner,
    policies: list[PromptPolicyRegistration],
) -> tuple[PromptPolicyRegistration, ...]:
    declarations = {item.id: item for item in manifest.contributes.prompt_policies}
    actual = _unique_contribution_map(
        "prompt policy",
        ((policy.policy_id, policy) for policy in policies),
    )
    _validate_declared_set("prompt policy", declarations, actual)
    permissions = {request.id for request in manifest.permissions}
    if actual and "sessions.prompt.inspect" not in permissions:
        raise ContributionValidationError(
            "prompt policies require sessions.prompt.inspect permission"
        )
    return tuple(
        replace(
            policy,
            plugin_id=manifest.plugin_id,
            priority=declarations[policy.policy_id].priority,
            timeout_seconds=declarations[policy.policy_id].timeout_seconds,
            owner=owner.key,
            generation_id=owner.generation_id,
            active_when_controller_id=_canonical_controller_id(
                manifest.plugin_id,
                declarations[policy.policy_id].active_when,
            ),
        )
        for policy in actual.values()
    )


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
            active_when_controller_id=_canonical_controller_id(
                manifest.plugin_id,
                declarations[policy.policy_id].active_when,
            ),
        )
        for policy in actual.values()
    )
