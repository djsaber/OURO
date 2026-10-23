"""Contribution declarations and prepared contribution records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from ouro.core.compaction import CompactionStrategy
from ouro.core.context_providers import ContextProviderRegistration
from ouro.core.continuations import ContinuationPolicyRegistration
from ouro.core.observers import ObserverRegistration
from ouro.core.prompt_policies import PromptPolicyRegistration
from ouro.core.tool_policies import ToolPolicyRegistration
from ouro.core.turn_policies import TurnPolicyRegistration
from ouro.plugins.models import PluginClass
from ouro.plugins.providers import ProviderRegistration
from ouro.plugins.registry import PluginCommand
from ouro.plugins.run_drivers import RunDriverRegistration
from ouro.plugins.session_controllers import PluginSessionController
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
    compaction_strategies: list[CompactionStrategy] = field(default_factory=list)
    background_services: list[BackgroundService] = field(default_factory=list)
    providers: list[ProviderRegistration] = field(default_factory=list)
    run_drivers: list[RunDriverRegistration] = field(default_factory=list)
    continuation_policies: list[ContinuationPolicyRegistration] = field(default_factory=list)
    prompt_policies: list[PromptPolicyRegistration] = field(default_factory=list)
    turn_policies: list[TurnPolicyRegistration] = field(default_factory=list)
    tool_policies: list[ToolPolicyRegistration] = field(default_factory=list)
    session_controllers: list[PluginSessionController] = field(default_factory=list)


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
    contract_hashes: dict[str, str] = field(default_factory=dict)
    degraded_reasons: tuple[str, ...] = ()


class ContributionValidationError(ValueError):
    pass
