"""Prepare each contribution type against host capabilities."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from ouro.plugins.contributions.models import ContributionValidationError, PreparedBackgroundService
from ouro.plugins.contributions.preparation import (
    ContributionCoordinator,
    _missing_optional,
    _PreparationContext,
    _PreparedBuilder,
    _unique_contribution_map,
    _validate_declared_set,
)


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


@dataclass(frozen=True)
class _ToolPreparationAdapter:
    kind: str = "tools"

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None:
        builder.tools = coordinator.prepare_tools(
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
class _ObserverPreparationAdapter:
    kind: str = "observers"

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None:
        builder.observers = (
            *builder.observers,
            *coordinator._prepare_observers(
                context.candidate.manifest,
                context.owner,
                context.source,
                context.contributions.observers,
            ),
        )

    def missing_optional(
        self,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> tuple[str, ...]:
        return _missing_optional(
            "observers",
            context.candidate.manifest.contributes.observers,
            {observer.observer_id for observer in builder.observers},
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
class _SessionControllerPreparationAdapter:
    kind: str = "session_controllers"

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None:
        builder.session_controllers = coordinator._prepare_session_controllers(
            context.candidate.manifest,
            context.owner,
            context.source,
            context.contributions.session_controllers,
        )

    def missing_optional(
        self,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> tuple[str, ...]:
        return _missing_optional(
            "session_controllers",
            context.candidate.manifest.contributes.session_controllers,
            {controller.controller_id for controller in builder.session_controllers},
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
        declarations = {
            item.id: item for item in context.candidate.manifest.contributes.background_services
        }
        builder.background_services = tuple(
            PreparedBackgroundService(service, declarations[service.service_id].scope)
            for service in context.contributions.background_services
        )

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
class _PromptPolicyPreparationAdapter:
    kind: str = "prompt_policies"

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None:
        builder.prompt_policies = coordinator._prepare_prompt_policies(
            context.candidate.manifest,
            context.owner,
            context.contributions.prompt_policies,
        )

    def missing_optional(
        self,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> tuple[str, ...]:
        return _missing_optional(
            "prompt_policies",
            context.candidate.manifest.contributes.prompt_policies,
            {policy.policy_id for policy in builder.prompt_policies},
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


OBSERVER_PREPARATION_ADAPTER = _ObserverPreparationAdapter()


COMMAND_PREPARATION_ADAPTER = _CommandPreparationAdapter()


SESSION_CONTROLLER_PREPARATION_ADAPTER = _SessionControllerPreparationAdapter()


CONTEXT_PREPARATION_ADAPTER = _ContextPreparationAdapter()


COMPACTION_PREPARATION_ADAPTER = _CompactionPreparationAdapter()


SKILL_PREPARATION_ADAPTER = _SkillPreparationAdapter()


MCP_PREPARATION_ADAPTER = _MCPPreparationAdapter()


BACKGROUND_SERVICE_PREPARATION_ADAPTER = _BackgroundServicePreparationAdapter()


@dataclass(frozen=True)
class _RunDriverPreparationAdapter:
    kind: str = "run_drivers"

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None:
        manifest = context.candidate.manifest
        declarations = {item.id: item for item in manifest.contributes.run_drivers}
        actual = _unique_contribution_map(
            "Run driver", ((item.driver_id, item) for item in context.contributions.run_drivers)
        )
        _validate_declared_set("Run driver", declarations, actual)
        if actual and "sessions.write" not in {item.id for item in manifest.permissions}:
            raise ContributionValidationError("Run drivers require sessions.write permission")
        builder.run_drivers = tuple(
            replace(
                item,
                plugin_id=manifest.plugin_id,
                release_id=context.owner.release_id,
                owner=context.owner.key,
            )
            for item in actual.values()
        )

    def missing_optional(
        self, context: _PreparationContext, builder: _PreparedBuilder
    ) -> tuple[str, ...]:
        return _missing_optional(
            "run_drivers",
            context.candidate.manifest.contributes.run_drivers,
            {item.driver_id for item in builder.run_drivers},
        )


@dataclass(frozen=True)
class _ProviderPreparationAdapter:
    kind: str = "providers"

    def prepare(
        self,
        coordinator: ContributionCoordinator,
        context: _PreparationContext,
        builder: _PreparedBuilder,
    ) -> None:
        manifest = context.candidate.manifest
        declarations = {item.id: item for item in manifest.contributes.providers}
        actual = _unique_contribution_map(
            "Provider", ((item.provider_id, item) for item in context.contributions.providers)
        )
        _validate_declared_set("Provider", declarations, actual)
        builder.providers = tuple(
            replace(item, plugin_id=manifest.plugin_id, owner=context.owner.key)
            for item in actual.values()
        )

    def missing_optional(
        self, context: _PreparationContext, builder: _PreparedBuilder
    ) -> tuple[str, ...]:
        return _missing_optional(
            "providers",
            context.candidate.manifest.contributes.providers,
            {item.provider_id for item in builder.providers},
        )


PROVIDER_PREPARATION_ADAPTER = _ProviderPreparationAdapter()


RUN_DRIVER_PREPARATION_ADAPTER = _RunDriverPreparationAdapter()


CONTINUATION_PREPARATION_ADAPTER = _ContinuationPreparationAdapter()


TOOL_POLICY_PREPARATION_ADAPTER = _ToolPolicyPreparationAdapter()


PROMPT_POLICY_PREPARATION_ADAPTER = _PromptPolicyPreparationAdapter()


TURN_POLICY_PREPARATION_ADAPTER = _TurnPolicyPreparationAdapter()
