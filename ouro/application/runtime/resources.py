"""In-memory resources owned by one Run, independent of its durable record."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ouro.core.agents import (
    AgentCatalogSnapshot,
    DelegatedConstraints,
    DelegationRecord,
    EffectiveToolRef,
)
from ouro.core.run_budget import RunBudgetPolicy

if TYPE_CHECKING:
    from ouro.application.tool_calls import ToolCallCoordinator
    from ouro.plugins.catalog.snapshot import ExtensionCatalogLease
    from ouro.tools.registry import ToolRegistry


@dataclass
class RunResources:
    snapshot: ExtensionCatalogLease | None = None
    host_tools: ToolRegistry | None = None
    controller_ids: frozenset[str] = frozenset()
    agent_catalog: AgentCatalogSnapshot | None = None
    tool_names: frozenset[str] | None = None
    tool_refs: tuple[EffectiveToolRef, ...] = ()
    constraints: DelegatedConstraints | None = None
    agent_prompt: str = ""
    budget: RunBudgetPolicy | None = None
    tool_calls: ToolCallCoordinator | None = None

    def bind_delegation(self, relation: DelegationRecord | None) -> None:
        self.tool_names = (
            frozenset(tool.public_name for tool in relation.effective_tools)
            if relation is not None
            else None
        )
        self.tool_refs = relation.effective_tools if relation is not None else ()
        self.constraints = relation.constraints if relation is not None else None
        self.agent_prompt = relation.definition.system_prompt if relation is not None else ""
        self.budget = relation.budget if relation is not None else None

    def take_snapshot(self) -> ExtensionCatalogLease | None:
        """Transfer the lease without closing it; all other state stays Run-owned."""
        snapshot, self.snapshot = self.snapshot, None
        return snapshot
