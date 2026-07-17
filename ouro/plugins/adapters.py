from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from ouro.core.compaction import CompactionStrategyRegistry
from ouro.core.continuations import RunContinuationRegistry
from ouro.core.hooks import HookRegistry
from ouro.core.tool_policies import ToolPolicyRegistry
from ouro.plugins.contributions import PreparedContributionSet
from ouro.plugins.registry import PluginCommandRegistry
from ouro.skills import SkillMetadata
from ouro.tools import ToolRegistry

ContributionLifecycle = Literal["snapshot-bound", "activation-singleton"]


@dataclass(frozen=True)
class CatalogProjectionTargets:
    tools: ToolRegistry
    hooks: HookRegistry
    commands: PluginCommandRegistry
    compaction: CompactionStrategyRegistry
    continuations: RunContinuationRegistry
    tool_policies: ToolPolicyRegistry
    skills: dict[str, SkillMetadata]


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
HOOK_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "hooks_context_skills",
    "hooks",
    "hooks",
)
COMMAND_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "commands",
    "commands",
    "commands",
)
COMPACTION_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "compaction_strategies",
    "compaction",
    "compaction_strategies",
)
CONTINUATION_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "continuation_policies",
    "continuations",
    "continuation_policies",
)
TOOL_POLICY_PROJECTION_ADAPTER = _SnapshotProjectionAdapter(
    "tool_policies",
    "tool_policies",
    "tool_policies",
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
    "project_prepared_contributions",
]
