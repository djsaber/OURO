"""Contribution declarations and prepared contribution records."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ouro.core.compaction import CompactionStrategy
from ouro.core.context_providers import ContextProviderRegistration
from ouro.core.continuations import ContinuationPolicyRegistration
from ouro.core.observers import ObserverRegistration
from ouro.core.prompt_policies import PromptPolicyRegistration
from ouro.core.run_drivers import RunDriverRegistration
from ouro.core.session_controller_registry import SessionController
from ouro.core.system_prompts import SystemPromptRegistration
from ouro.core.tool_policies import ToolPolicyRegistration
from ouro.core.turn_policies import TurnPolicyRegistration
from ouro.plugins.models import PluginClass
from ouro.plugins.providers import ProviderRegistration
from ouro.plugins.registry import PluginCommand
from ouro.skills import SkillMetadata
from ouro.tools import Tool


class BackgroundService(Protocol):
    service_id: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


type BackgroundServiceScope = Literal["host", "home"]


@dataclass(frozen=True)
class PreparedBackgroundService:
    implementation: BackgroundService
    scope: BackgroundServiceScope

    @property
    def service_id(self) -> str:
        return self.implementation.service_id

    async def start(self) -> None:
        await self.implementation.start()

    async def stop(self) -> None:
        await self.implementation.stop()


@dataclass
class PluginContributions:
    tools: list[Tool] = field(default_factory=list)
    observers: list[ObserverRegistration] = field(default_factory=list)
    commands: list[PluginCommand] = field(default_factory=list)
    context_providers: list[ContextProviderRegistration] = field(default_factory=list)
    system_prompt_providers: list[SystemPromptRegistration] = field(default_factory=list)
    compaction_strategies: list[CompactionStrategy] = field(default_factory=list)
    background_services: list[BackgroundService] = field(default_factory=list)
    providers: list[ProviderRegistration] = field(default_factory=list)
    run_drivers: list[RunDriverRegistration] = field(default_factory=list)
    continuation_policies: list[ContinuationPolicyRegistration] = field(default_factory=list)
    prompt_policies: list[PromptPolicyRegistration] = field(default_factory=list)
    turn_policies: list[TurnPolicyRegistration] = field(default_factory=list)
    tool_policies: list[ToolPolicyRegistration] = field(default_factory=list)
    session_controllers: list[SessionController] = field(default_factory=list)


@dataclass(frozen=True)
class ContributionOwner:
    plugin_id: str
    plugin_class: PluginClass
    release_id: str
    generation_id: str
    package_digest: str | None = None

    @property
    def key(self) -> str:
        return f"{self.plugin_id}@{self.release_id}#{self.generation_id}"


@dataclass(frozen=True)
class PreparedContributionSet:
    owner: ContributionOwner
    tools: tuple[Tool, ...] = ()
    observers: tuple[ObserverRegistration, ...] = ()
    context_providers: tuple[ContextProviderRegistration, ...] = ()
    system_prompt_providers: tuple[SystemPromptRegistration, ...] = ()
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
    session_controllers: tuple[SessionController, ...] = ()
    mcp_server_ids: tuple[str, ...] = ()
    degraded_reasons: tuple[str, ...] = ()

    def identities(self) -> Iterator[str]:
        """Canonical names claimed by the contributions actually prepared for publication."""
        plugin = self.owner.plugin_id
        yield from (tool.definition.identity for tool in self.tools)
        yield from (provider.canonical_id for provider in self.providers)
        yield from (driver.canonical_id for driver in self.run_drivers)
        yield from (controller.canonical_id for controller in self.session_controllers)
        yield from (f"{plugin}/observers/{item.observer_id}" for item in self.observers)
        yield from (f"{plugin}/commands/{item.name}" for item in self.commands)
        yield from (f"{plugin}/skills/{item.name}" for item in self.skills)
        yield from (f"{plugin}/mcp_servers/{name}" for name in self.mcp_server_ids)
        yield from (
            f"{plugin}/background_services/{item.service_id}" for item in self.background_services
        )
        for strategy in self.compaction_strategies:
            local_id = strategy.strategy_id.removeprefix(plugin + ".")
            yield f"{plugin}/compaction_strategies/{local_id}"
        for kind, providers in (
            ("context_providers", self.context_providers),
            ("system_prompt_providers", self.system_prompt_providers),
        ):
            yield from (f"{plugin}/{kind}/{item.provider_id}" for item in providers)
        for kind, policies in (
            ("continuation_policies", self.continuation_policies),
            ("prompt_policies", self.prompt_policies),
            ("turn_policies", self.turn_policies),
            ("tool_policies", self.tool_policies),
        ):
            yield from (f"{plugin}/{kind}/{item.policy_id}" for item in policies)


class ContributionValidationError(ValueError):
    pass
