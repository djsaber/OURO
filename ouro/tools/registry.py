from __future__ import annotations

from ouro.tools.base import Tool, ToolDefinition


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.definition.name in self._tools:
            raise ValueError(f"tool already registered: {tool.definition.name}")
        self._tools[tool.definition.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def definitions(self) -> list[ToolDefinition]:
        return [tool.definition for tool in self._tools.values()]

    def names(self) -> list[str]:
        return sorted(self._tools)
