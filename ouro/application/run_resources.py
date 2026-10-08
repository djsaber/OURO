"""In-memory resources owned by one Run, independent of its durable record."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ouro.core.agents import AgentCatalogSnapshot, DelegatedConstraints, EffectiveToolRef
from ouro.core.state import RunRecord

if TYPE_CHECKING:
    from ouro.application.tool_calls import ToolCallCoordinator
    from ouro.plugins.catalog import ExtensionCatalogLease
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
    tool_calls: ToolCallCoordinator | None = None

    def restore_tool_bindings(self, run: RunRecord) -> None:
        if "effective_tool_names" not in run.metadata:
            return
        raw = run.metadata.get("effective_tool_names")
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise ValueError("run has invalid effective tool names")
        raw_refs = run.metadata.get("effective_tool_refs", [])
        if not isinstance(raw_refs, list) or any(not isinstance(item, dict) for item in raw_refs):
            raise ValueError("run has invalid effective tool references")
        refs = tuple(EffectiveToolRef.model_validate(item) for item in raw_refs)
        raw_constraints = run.metadata.get("delegated_constraints", {})
        if not isinstance(raw_constraints, dict):
            raise ValueError("run has invalid delegated constraints")
        constraints = DelegatedConstraints.model_validate(raw_constraints)
        self.tool_names = frozenset(raw)
        self.tool_refs = refs
        self.constraints = constraints

    def take_snapshot(self) -> ExtensionCatalogLease | None:
        """Transfer the lease without closing it; all other state stays Run-owned."""
        snapshot, self.snapshot = self.snapshot, None
        return snapshot
