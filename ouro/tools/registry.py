from __future__ import annotations

from collections.abc import Callable, Iterable

from ouro.core.agents import AGENT_CONTROL_TOOL_NAMES
from ouro.tools.base import Tool, ToolDefinition
from ouro.tools.schema import validate_tool_definition


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._owners: dict[str, str] = {}
        self._frozen = False
        self._active_controller_ids: Callable[[], set[str]] | None = None

    def freeze(self) -> None:
        self._frozen = True

    def _require_mutable(self) -> None:
        if self._frozen:
            raise RuntimeError("tool registry structure is frozen")

    def register(self, tool: Tool) -> None:
        self.replace((), (tool,))

    def replace(self, previous_names: Iterable[str], tools: Iterable[Tool]) -> None:
        """Atomically replace a contribution set after validating every tool."""

        self._require_mutable()
        previous = set(previous_names)
        additions = list(tools)
        addition_names = [tool.definition.name for tool in additions]
        if len(addition_names) != len(set(addition_names)):
            raise ValueError("replacement contains duplicate tool names")
        for tool in additions:
            _validate_tool(tool)

        updated = dict(self._tools)
        owners = dict(self._owners)
        for name in previous:
            updated.pop(name, None)
            owners.pop(name, None)
        for tool in additions:
            name = tool.definition.name
            if name in updated:
                raise ValueError(f"tool already registered: {name}")
            updated[name] = tool
        self._tools = updated
        self._owners = owners

    def replace_owner(self, owner: str, tools: Iterable[Tool]) -> None:
        """Atomically replace every tool owned by one extension generation."""

        self._require_mutable()
        additions = list(tools)
        names = [tool.definition.name for tool in additions]
        if len(names) != len(set(names)):
            raise ValueError("replacement contains duplicate tool names")
        for tool in additions:
            _validate_tool(tool)
        updated = {
            name: tool for name, tool in self._tools.items() if self._owners.get(name) != owner
        }
        owners = {
            name: item_owner for name, item_owner in self._owners.items() if item_owner != owner
        }
        for tool in additions:
            name = tool.definition.name
            if name in updated:
                conflict = owners.get(name, "builtin")
                raise ValueError(f"tool already registered: {name} (owner: {conflict})")
            updated[name] = tool
            owners[name] = owner
        self._tools = updated
        self._owners = owners

    def unregister_owner(self, owner: str) -> None:
        self.replace_owner(owner, ())

    def snapshot(self) -> tuple[dict[str, Tool], dict[str, str]]:
        return dict(self._tools), dict(self._owners)

    def restore(self, snapshot: tuple[dict[str, Tool], dict[str, str]]) -> None:
        self._require_mutable()
        self._tools, self._owners = dict(snapshot[0]), dict(snapshot[1])

    def unregister(self, names: Iterable[str]) -> None:
        self.replace(names, ())

    def get(self, name: str) -> Tool:
        try:
            tool = self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc
        if not self._is_visible(tool):
            raise KeyError(f"inactive tool: {name}")
        return tool

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values() if self._is_visible(tool)]

    def names(self) -> list[str]:
        return sorted(name for name, tool in self._tools.items() if self._is_visible(tool))

    def owner_of(self, name: str) -> str | None:
        if name in self._tools and not self._is_visible(self._tools[name]):
            return None
        return self._owners.get(name)

    def scoped(self, active_controller_ids: Callable[[], set[str]]) -> ToolRegistry:
        scoped = ToolRegistry()
        scoped.restore(self.snapshot())
        scoped._active_controller_ids = active_controller_ids
        scoped.freeze()
        return scoped

    def selected(self, names: Iterable[str]) -> ToolRegistry:
        """Freeze an exact name subset without changing the owning registry."""

        selected_names = tuple(dict.fromkeys(names))
        missing = [name for name in selected_names if name not in self._tools]
        if missing:
            raise KeyError("unknown tools in selected registry: " + ", ".join(missing))
        selected = ToolRegistry()
        selected._tools = {name: self._tools[name] for name in selected_names}
        selected._owners = {
            name: self._owners[name] for name in selected_names if name in self._owners
        }
        selected._active_controller_ids = self._active_controller_ids
        selected.freeze()
        return selected

    def overlay(self, tools: Iterable[Tool]) -> ToolRegistry:
        """Return a frozen per-Run view with product-owned dynamic tools."""

        additions = list(tools)
        names = [tool.definition.name for tool in additions]
        if len(names) != len(set(names)):
            raise ValueError("tool overlay contains duplicate names")
        for tool in additions:
            _validate_tool(tool)
        conflicts = sorted(set(names).intersection(self._tools))
        if conflicts:
            raise ValueError(
                "tool overlay conflicts with registered tools: " + ", ".join(conflicts)
            )
        overlaid = ToolRegistry()
        overlaid._tools = {**self._tools, **{tool.definition.name: tool for tool in additions}}
        overlaid._owners = dict(self._owners)
        overlaid._active_controller_ids = self._active_controller_ids
        overlaid.freeze()
        return overlaid

    def _is_visible(self, tool: Tool) -> bool:
        controller_id = tool.definition.active_when_controller_id
        return (
            controller_id is None
            or self._active_controller_ids is None
            or controller_id in self._active_controller_ids()
        )


def _validate_tool(tool: Tool) -> None:
    validate_tool_definition(tool.definition)
    if (
        tool.definition.name in AGENT_CONTROL_TOOL_NAMES
        and tool.definition.metadata.get("ouro_agent_control") is not True
    ):
        raise ValueError(f"tool name is reserved for OURO Agent control: {tool.definition.name}")
    if tool.definition.resumable is not (tool.resume_handler is not None):
        raise ValueError(
            f"tool resumable contract does not match resume handler: {tool.definition.name}"
        )
