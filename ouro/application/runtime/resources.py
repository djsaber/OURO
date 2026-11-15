"""In-memory resources owned by one Run, independent of its durable record."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ouro.application.agents.definitions import AgentDefinitionService
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

    def select_tools(
        self,
        host_tools: ToolRegistry,
        active_controller_ids: Callable[[], set[str]],
    ) -> ToolRegistry:
        """Apply this Run's controller scope, delegation ceiling and Agent controls."""
        if self.snapshot is not None:
            registry = self.snapshot.tools
        else:
            registry = self.host_tools if self.host_tools is not None else host_tools
        scoped = registry.scoped(active_controller_ids)
        selected = (
            scoped
            if self.tool_names is None
            else scoped.selected(self.tool_names.intersection(scoped.names()))
        )
        if self.tool_refs:
            ceilings = {reference.public_name: reference for reference in self.tool_refs}
            selected = selected.selected(
                definition.name
                for definition in selected.definitions()
                if definition.name in ceilings
                and ceilings[definition.name].permits(
                    AgentDefinitionService.tool_reference(selected, definition)
                )
            )
        return selected.overlay(self.agent_tools) if self.agent_tools is not None else selected

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
