"""In-memory resources owned by one Run, independent of its durable record."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ouro.core.agents import (
    AgentCatalogSnapshot,
    DelegationRecord,
    EffectiveToolRef,
)
from ouro.core.run_budget import RunBudgetPolicy
from ouro.tools.builtin.agents import agent_control_tools
from ouro.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from ouro.application.runtime.extensions import RuntimeCapabilities
    from ouro.application.tool_calls import ToolCallCoordinator


@dataclass
class RunResources:
    snapshot: RuntimeCapabilities | None = None
    host_tools: ToolRegistry | None = None
    controller_ids: frozenset[str] = frozenset()
    agent_catalog: AgentCatalogSnapshot | None = None
    agent_tools: ToolRegistry | None = None
    tool_names: frozenset[str] | None = None
    tool_refs: tuple[EffectiveToolRef, ...] = ()
    agent_prompt: str = ""
    budget: RunBudgetPolicy | None = None
    tool_calls: ToolCallCoordinator | None = None

    def bind_agent_catalog(self, catalog: AgentCatalogSnapshot) -> None:
        """Own the validated control tools for this decision's declared capabilities."""
        if (
            self.agent_catalog is not None
            and self.agent_catalog.contract_hash == catalog.contract_hash
        ):
            return
        tools = ToolRegistry()
        tools.replace((), agent_control_tools(catalog))
        tools.freeze()
        self.agent_catalog, self.agent_tools = catalog, tools

    def bind_delegation(self, relation: DelegationRecord | None) -> None:
        self.tool_names = (
            frozenset(tool.public_name for tool in relation.effective_tools)
            if relation is not None
            else None
        )
        self.tool_refs = relation.effective_tools if relation is not None else ()
        self.agent_prompt = relation.definition.system_prompt if relation is not None else ""
        self.budget = relation.budget if relation is not None else None

    def take_snapshot(self) -> RuntimeCapabilities | None:
        """Transfer the lease without closing it; all other state stays Run-owned."""
        snapshot, self.snapshot = self.snapshot, None
        return snapshot
