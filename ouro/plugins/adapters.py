from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from ouro.core.compaction import CompactionStrategyRegistry
from ouro.core.context_providers import ContextProviderRegistry
from ouro.core.continuations import RunContinuationRegistry
from ouro.core.observers import ObserverRegistry
from ouro.core.prompt_policies import PromptPolicyRegistry
from ouro.core.tool_policies import ToolPolicyRegistry
from ouro.core.turn_policies import TurnPolicyRegistry
from ouro.plugins.contributions import PreparedContributionSet
from ouro.plugins.providers import ProviderRegistry
from ouro.plugins.registry import PluginCommandRegistry
from ouro.plugins.run_drivers import RunDriverRegistry
from ouro.plugins.session_controllers import PluginSessionControllerRegistry
from ouro.skills import SkillMetadata
from ouro.tools import ToolRegistry

ContributionLifecycle = Literal["snapshot-bound", "activation-singleton"]


@dataclass(frozen=True)
class CatalogProjectionTargets:
    tools: ToolRegistry
    observers: ObserverRegistry
    context_providers: ContextProviderRegistry
    commands: PluginCommandRegistry
    compaction: CompactionStrategyRegistry
    providers: ProviderRegistry
    run_drivers: RunDriverRegistry
    continuations: RunContinuationRegistry
    prompt_policies: PromptPolicyRegistry
    turn_policies: TurnPolicyRegistry
    tool_policies: ToolPolicyRegistry
    skills: dict[str, SkillMetadata]
    session_controllers: PluginSessionControllerRegistry


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
    ) -> None: ...


@dataclass(frozen=True)
class _SnapshotProjectionAdapter:
    kind: str
    target_attribute: str
    prepared_attribute: str
    lifecycle: ContributionLifecycle = "snapshot-bound"

    def project(
        self,
        targets: CatalogProjectionTargets,
        *,
        owner: str,
        prepared: PreparedContributionSet,
    ) -> None:
        registry = getattr(targets, self.target_attribute)
        contributions = getattr(prepared, self.prepared_attribute)
        registry.replace_owner(owner, contributions)


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
    ) -> None:
        del targets, owner, prepared


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
    ) -> None:
        for skill in prepared.skills:
            if skill.name in targets.skills:
                raise ValueError(f"catalog skill name conflict: {skill.name} ({owner})")
            targets.skills[skill.name] = skill


TOOL_PROJECTION_ADAPTER = _SnapshotProjectionAdapter("tools", "tools", "tools")
OBSERVER_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "observers",
    "observers",
    "observers",
)
CONTEXT_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "context_providers",
    "context_providers",
    "context_providers",
)
COMMAND_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "commands",
    "commands",
    "commands",
)
SESSION_CONTROLLER_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "session_controllers",
    "session_controllers",
    "session_controllers",
)
COMPACTION_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "compaction_strategies",
    "compaction",
    "compaction_strategies",
)
PROVIDER_PROJECTION_ADAPTER = _SnapshotProjectionAdapter("providers", "providers", "providers")
RUN_DRIVER_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "run_drivers", "run_drivers", "run_drivers"
)
CONTINUATION_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "continuation_policies",
    "continuations",
    "continuation_policies",
)
PROMPT_POLICY_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "prompt_policies",
    "prompt_policies",
    "prompt_policies",
)
TOOL_POLICY_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "tool_policies",
    "tool_policies",
    "tool_policies",
)
TURN_POLICY_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "turn_policies",
    "turn_policies",
    "turn_policies",
)
BACKGROUND_SERVICE_PROJECTION_ADAPTER = _BackgroundServiceAdapter()
SKILL_PROJECTION_ADAPTER = _SkillProjectionAdapter()


def project_prepared_contributions(
    targets: CatalogProjectionTargets,
    *,
    owner: str,
    prepared: PreparedContributionSet,
) -> None:
    from ouro.plugins.contribution_types import contribution_type_adapters

    projected: set[int] = set()
    for contribution_type in contribution_type_adapters():
        adapter = contribution_type.projection
        if adapter is None or id(adapter) in projected:
            continue
        projected.add(id(adapter))
        adapter.project(targets, owner=owner, prepared=prepared)


__all__ = [
    "CatalogProjectionTargets",
    "ContributionLifecycle",
    "ContributionProjectionAdapter",
    "CONTEXT_PROJECTION_ADAPTER",
    "project_prepared_contributions",
]
