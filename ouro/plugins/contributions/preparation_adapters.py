"""Prepare native contributions and implement the two request-provider catalog adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from operator import attrgetter
from typing import Any, Literal, Protocol

from ouro.core.callbacks import invoke_owned_callback
from ouro.core.request_providers import RequestProviderRegistration
from ouro.plugins.catalog.gates import GenerationGate
from ouro.plugins.contributions.models import (
    ContributionValidationError,
    PreparedBackgroundService,
    PreparedContributionSet,
)
from ouro.plugins.contributions.preparation_rules import (
    _canonical_controller_id,
    _missing_optional,
    _PreparationContext,
    _prepare_commands,
    _prepare_compaction,
    _prepare_continuation_policies,
    _prepare_discovered_tools,
    _prepare_observers,
    _prepare_prompt_policies,
    _prepare_session_controllers,
    _prepare_tool_policies,
    _prepare_turn_policies,
    _unique_contribution_map,
    _validate_background_services,
    _validate_declared_set,
    prepare_tools,
)
from ouro.plugins.contributions.projection_adapters import CatalogProjectionTargets
from ouro.plugins.manifest import PluginManifest


class ContributionPreparationAdapter(Protocol):
    @property
    def kind(self) -> str: ...

    def prepare(
        self,
        context: _PreparationContext,
        items: dict[str, tuple],
    ) -> None: ...

    def missing_optional(
        self,
        context: _PreparationContext,
        prepared: PreparedContributionSet,
    ) -> tuple[str, ...]: ...


def _bind_provider[R: RequestProviderRegistration](registration: R, gate: GenerationGate) -> R:
    async def invoke(call: Any) -> Any:
        async with gate.invocation():
            return await invoke_owned_callback(registration.handler, call)

    return replace(registration, handler=invoke)


@dataclass(frozen=True)
class RequestProviderContribution:
    kind: Literal["system_prompt_providers", "context_providers"]
    lifecycle: Literal["snapshot-bound"] = "snapshot-bound"

    def prepare(self, context: _PreparationContext, items: dict[str, tuple]) -> None:
        manifest = context.manifest
        declarations = {item.id: item for item in getattr(manifest.contributes, self.kind)}
        actual = _unique_contribution_map(
            self.kind,
            ((item.provider_id, item) for item in getattr(context.contributions, self.kind)),
        )
        _validate_declared_set(self.kind, declarations, actual)
        registrations = []
        for identity, provider in actual.items():
            declaration = declarations[identity]
            registrations.append(
                replace(
                    provider,
                    plugin_id=manifest.plugin_id,
                    priority=declaration.priority,
                    timeout_seconds=declaration.timeout_seconds,
                    failure_mode=declaration.failure_mode,
                    owner=context.owner.key,
                    generation_id=context.owner.generation_id,
                    active_when_controller_id=_canonical_controller_id(
                        manifest.plugin_id, declaration.active_when
                    ),
                )
            )
        items[self.kind] = tuple(registrations)

    def missing_optional(
        self, context: _PreparationContext, prepared: PreparedContributionSet
    ) -> tuple[str, ...]:
        return _missing_optional(
            self.kind,
            getattr(context.manifest.contributes, self.kind),
            {item.provider_id for item in getattr(prepared, self.kind)},
        )

    def contracts(
        self, manifest: PluginManifest, prepared: PreparedContributionSet
    ) -> dict[str, object]:
        return {
            f"{manifest.plugin_id}/{self.kind}/{provider.provider_id}": {
                "priority": provider.priority,
                "timeout_seconds": provider.timeout_seconds,
                "failure_mode": provider.failure_mode,
                "active_when_controller_id": provider.active_when_controller_id,
            }
            for provider in getattr(prepared, self.kind)
        }

    def project(
        self,
        targets: CatalogProjectionTargets,
        *,
        owner: str,
        prepared: PreparedContributionSet,
        gate: GenerationGate,
    ) -> None:
        registry = getattr(targets, self.kind)
        registry.replace_owner(
            owner, tuple(_bind_provider(item, gate) for item in getattr(prepared, self.kind))
        )


CONTEXT_PROVIDERS = RequestProviderContribution("context_providers")
SYSTEM_PROMPT_PROVIDERS = RequestProviderContribution("system_prompt_providers")


@dataclass(frozen=True)
class _ToolPreparationAdapter:
    kind: str = "tools"

    def prepare(
        self,
        context: _PreparationContext,
        items: dict[str, tuple],
    ) -> None:
        registered = prepare_tools(
            context.manifest,
            context.owner,
            context.source,
            context.contributions.tools,
        )
        discovered = _prepare_discovered_tools(
            context.manifest, context.owner, context.discovered_tools
        )
        items["tools"] = (*registered, *discovered)

    def missing_optional(
        self,
        context: _PreparationContext,
        prepared: PreparedContributionSet,
    ) -> tuple[str, ...]:
        del prepared
        return _missing_optional(
            "tools",
            context.manifest.contributes.tools,
            {tool.definition.name for tool in context.contributions.tools},
            exposed_name=True,
        )


@dataclass(frozen=True)
class _RegistrationPreparation[R]:
    kind: str
    registrations: Callable[[_PreparationContext], tuple[R, ...]]
    identity: Callable[[R], str]

    def prepare(self, context: _PreparationContext, items: dict[str, tuple]) -> None:
        items[self.kind] = self.registrations(context)

    def missing_optional(
        self, context: _PreparationContext, prepared: PreparedContributionSet
    ) -> tuple[str, ...]:
        return _missing_optional(
            self.kind,
            getattr(context.manifest.contributes, self.kind),
            {self.identity(item) for item in getattr(prepared, self.kind)},
        )


@dataclass(frozen=True)
class _CompactionPreparationAdapter:
    kind: str = "compaction_strategies"

    def prepare(
        self,
        context: _PreparationContext,
        items: dict[str, tuple],
    ) -> None:
        items["compaction_strategies"] = _prepare_compaction(
            context.manifest,
            context.contributions.compaction_strategies,
        )

    def missing_optional(
        self,
        context: _PreparationContext,
        prepared: PreparedContributionSet,
    ) -> tuple[str, ...]:
        return _missing_optional(
            "compaction_strategies",
            context.manifest.contributes.compaction_strategies,
            {
                strategy.strategy_id.removeprefix(f"{context.manifest.plugin_id}.")
                for strategy in prepared.compaction_strategies
            },
        )


@dataclass(frozen=True)
class _SkillPreparationAdapter:
    kind: str = "skills"

    def prepare(
        self,
        context: _PreparationContext,
        items: dict[str, tuple],
    ) -> None:
        items["skills"] = context.resource_skills

    def missing_optional(
        self,
        context: _PreparationContext,
        prepared: PreparedContributionSet,
    ) -> tuple[str, ...]:
        return _missing_optional(
            "skills",
            context.manifest.contributes.skills,
            {skill.name for skill in prepared.skills},
        )


@dataclass(frozen=True)
class _BackgroundServicePreparationAdapter:
    kind: str = "background_services"

    def prepare(
        self,
        context: _PreparationContext,
        items: dict[str, tuple],
    ) -> None:
        _validate_background_services(
            context.manifest,
            context.contributions.background_services,
        )
        declarations = {item.id: item for item in context.manifest.contributes.background_services}
        items["background_services"] = tuple(
            PreparedBackgroundService(service, declarations[service.service_id].scope)
            for service in context.contributions.background_services
        )

    def missing_optional(
        self,
        context: _PreparationContext,
        prepared: PreparedContributionSet,
    ) -> tuple[str, ...]:
        return _missing_optional(
            "background_services",
            context.manifest.contributes.background_services,
            {service.service_id for service in prepared.background_services},
        )


TOOL_PREPARATION_ADAPTER = _ToolPreparationAdapter()


OBSERVER_PREPARATION_ADAPTER = _RegistrationPreparation(
    "observers", _prepare_observers, attrgetter("observer_id")
)


COMMAND_PREPARATION_ADAPTER = _RegistrationPreparation(
    "commands", _prepare_commands, attrgetter("name")
)


SESSION_CONTROLLER_PREPARATION_ADAPTER = _RegistrationPreparation(
    "session_controllers", _prepare_session_controllers, attrgetter("controller_id")
)


COMPACTION_PREPARATION_ADAPTER = _CompactionPreparationAdapter()


SKILL_PREPARATION_ADAPTER = _SkillPreparationAdapter()


BACKGROUND_SERVICE_PREPARATION_ADAPTER = _BackgroundServicePreparationAdapter()


@dataclass(frozen=True)
class _RunDriverPreparationAdapter:
    kind: str = "run_drivers"

    def prepare(
        self,
        context: _PreparationContext,
        items: dict[str, tuple],
    ) -> None:
        manifest = context.manifest
        declarations = {item.id: item for item in manifest.contributes.run_drivers}
        actual = _unique_contribution_map(
            "Run driver", ((item.driver_id, item) for item in context.contributions.run_drivers)
        )
        _validate_declared_set("Run driver", declarations, actual)
        if actual and "sessions.write" not in {item.id for item in manifest.permissions}:
            raise ContributionValidationError("Run drivers require sessions.write permission")
        items["run_drivers"] = tuple(
            replace(
                item,
                plugin_id=manifest.plugin_id,
                release_id=context.owner.release_id,
                owner=context.owner.key,
            )
            for item in actual.values()
        )

    def missing_optional(
        self, context: _PreparationContext, prepared: PreparedContributionSet
    ) -> tuple[str, ...]:
        return _missing_optional(
            "run_drivers",
            context.manifest.contributes.run_drivers,
            {item.driver_id for item in prepared.run_drivers},
        )


@dataclass(frozen=True)
class _ProviderPreparationAdapter:
    kind: str = "providers"

    def prepare(
        self,
        context: _PreparationContext,
        items: dict[str, tuple],
    ) -> None:
        manifest = context.manifest
        declarations = {item.id: item for item in manifest.contributes.providers}
        actual = _unique_contribution_map(
            "Provider", ((item.provider_id, item) for item in context.contributions.providers)
        )
        _validate_declared_set("Provider", declarations, actual)
        items["providers"] = tuple(
            replace(item, plugin_id=manifest.plugin_id, owner=context.owner.key)
            for item in actual.values()
        )

    def missing_optional(
        self, context: _PreparationContext, prepared: PreparedContributionSet
    ) -> tuple[str, ...]:
        return _missing_optional(
            "providers",
            context.manifest.contributes.providers,
            {item.provider_id for item in prepared.providers},
        )


PROVIDER_PREPARATION_ADAPTER = _ProviderPreparationAdapter()


RUN_DRIVER_PREPARATION_ADAPTER = _RunDriverPreparationAdapter()


CONTINUATION_PREPARATION_ADAPTER = _RegistrationPreparation(
    "continuation_policies", _prepare_continuation_policies, attrgetter("policy_id")
)


TOOL_POLICY_PREPARATION_ADAPTER = _RegistrationPreparation(
    "tool_policies", _prepare_tool_policies, attrgetter("policy_id")
)


PROMPT_POLICY_PREPARATION_ADAPTER = _RegistrationPreparation(
    "prompt_policies", _prepare_prompt_policies, attrgetter("policy_id")
)


TURN_POLICY_PREPARATION_ADAPTER = _RegistrationPreparation(
    "turn_policies", _prepare_turn_policies, attrgetter("policy_id")
)
