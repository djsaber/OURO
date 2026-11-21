"""Catalog projections for individual contribution types."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ouro.core.compaction import CompactionStrategyRegistry
from ouro.core.context_providers import ContextProviderRegistry
from ouro.core.continuations import RunContinuationRegistry
from ouro.core.observers import ObserverRegistry
from ouro.core.prompt_policies import PromptPolicyRegistry
from ouro.core.session_controller_registry import SessionControllerRegistry
from ouro.core.system_prompts import SystemPromptRegistry
from ouro.core.tool_policies import ToolPolicyRegistry
from ouro.core.turn_policies import TurnPolicyRegistry
from ouro.plugins.catalog import bindings
from ouro.plugins.catalog.gates import GenerationGate
from ouro.plugins.contributions.models import PreparedContributionSet
from ouro.plugins.providers import ProviderRegistry
from ouro.plugins.registry import PluginCommandRegistry
from ouro.plugins.run_drivers import RunDriverRegistry
from ouro.skills import SkillMetadata
from ouro.tools import ToolRegistry

ContributionLifecycle = Literal["snapshot-bound", "activation-singleton"]


@dataclass(frozen=True)
class CatalogProjectionTargets:
    tools: ToolRegistry
    observers: ObserverRegistry
    context_providers: ContextProviderRegistry
    system_prompt_providers: SystemPromptRegistry
    commands: PluginCommandRegistry
    compaction: CompactionStrategyRegistry
    providers: ProviderRegistry
    run_drivers: RunDriverRegistry
    continuations: RunContinuationRegistry
    prompt_policies: PromptPolicyRegistry
    turn_policies: TurnPolicyRegistry
    tool_policies: ToolPolicyRegistry
    skills: dict[str, SkillMetadata]
    session_controllers: SessionControllerRegistry


class ContributionProjectionAdapter(Protocol):
    """Project one prepared contribution family into a candidate Catalog."""

    @property
    def kind(self) -> str: ...

    @property
    def lifecycle(self) -> ContributionLifecycle: ...

    def project(
        self,
        targets: CatalogProjectionTargets,
        *,
        owner: str,
        prepared: PreparedContributionSet,
        gate: GenerationGate,
    ) -> None: ...


@dataclass(frozen=True)
class _SnapshotProjectionAdapter:
    kind: str
    bind: Callable[[Any, GenerationGate], Any]
    target_attribute: str | None = None
    lifecycle: ContributionLifecycle = "snapshot-bound"

    def project(
        self,
        targets: CatalogProjectionTargets,
        *,
        owner: str,
        prepared: PreparedContributionSet,
        gate: GenerationGate,
    ) -> None:
        registry = getattr(targets, self.target_attribute or self.kind)
        contributions = getattr(prepared, self.kind)
        registry.replace_owner(owner, tuple(self.bind(item, gate) for item in contributions))


@dataclass(frozen=True)
class _BackgroundServiceAdapter:
    kind: str = "background_services"
    lifecycle: ContributionLifecycle = "activation-singleton"

    def project(
        self,
        targets: CatalogProjectionTargets,
        *,
        owner: str,
        prepared: PreparedContributionSet,
        gate: GenerationGate,
    ) -> None:
        del targets, owner, prepared, gate


@dataclass(frozen=True)
class _SkillProjectionAdapter:
    kind: str = "skills"
    lifecycle: ContributionLifecycle = "snapshot-bound"

    def project(
        self,
        targets: CatalogProjectionTargets,
        *,
        owner: str,
        prepared: PreparedContributionSet,
        gate: GenerationGate,
    ) -> None:
        for skill in prepared.skills:
            if skill.name in targets.skills:
                raise ValueError(f"catalog skill name conflict: {skill.name} ({owner})")
            targets.skills[skill.name] = skill


TOOL_PROJECTION_ADAPTER = _SnapshotProjectionAdapter("tools", bindings.gate_tool)
OBSERVER_PROJECTION_ADAPTER = _SnapshotProjectionAdapter("observers", bindings.gate_observer)
COMMAND_PROJECTION_ADAPTER = _SnapshotProjectionAdapter("commands", bindings.gate_command)
SESSION_CONTROLLER_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "session_controllers", bindings.gate_session_controller
)
COMPACTION_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "compaction_strategies", bindings.GatedCompactionStrategy, "compaction"
)
PROVIDER_PROJECTION_ADAPTER = _SnapshotProjectionAdapter("providers", bindings.gate_provider)
RUN_DRIVER_PROJECTION_ADAPTER = _SnapshotProjectionAdapter("run_drivers", bindings.gate_driver)
CONTINUATION_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "continuation_policies", bindings.gate_continuation, "continuations"
)
PROMPT_POLICY_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "prompt_policies", bindings.gate_prompt_policy
)
TOOL_POLICY_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "tool_policies", bindings.gate_tool_policy
)
TURN_POLICY_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "turn_policies", bindings.gate_turn_policy
)
BACKGROUND_SERVICE_PROJECTION_ADAPTER = _BackgroundServiceAdapter()
SKILL_PROJECTION_ADAPTER = _SkillProjectionAdapter()
