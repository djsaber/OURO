from __future__ import annotations

from collections.abc import Iterable

from ouro.tools.base import Tool, ToolDefinition
from ouro.tools.schema import validate_tool_definition


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self.replace((), (tool,))

    def replace(self, previous_names: Iterable[str], tools: Iterable[Tool]) -> None:
        """Atomically replace a contribution set after validating every tool."""

        previous = set(previous_names)
        additions = list(tools)
        addition_names = [tool.definition.name for tool in additions]
        if len(addition_names) != len(set(addition_names)):
            raise ValueError("replacement contains duplicate tool names")
        for tool in additions:
            validate_tool_definition(tool.definition)

        updated = dict(self._tools)
        for name in previous:
            updated.pop(name, None)
        for tool in additions:
            name = tool.definition.name
            if name in updated:
                raise ValueError(f"tool already registered: {name}")
            updated[name] = tool
        self._tools = updated

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
