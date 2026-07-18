from __future__ import annotations

from collections.abc import Iterable

from ouro.tools.base import Tool, ToolDefinition
from ouro.tools.schema import validate_tool_definition


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._owners: dict[str, str] = {}
        self._frozen = False

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
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def owner_of(self, name: str) -> str | None:
        return self._owners.get(name)


def _validate_tool(tool: Tool) -> None:
    validate_tool_definition(tool.definition)
    if tool.definition.resumable is not (tool.resume_handler is not None):
        raise ValueError(
            f"tool resumable contract does not match resume handler: {tool.definition.name}"
        )
