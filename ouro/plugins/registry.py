from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ouro.core.hooks import HookContext, HookResult, PluginSource

PluginCommandHandler = Callable[
    [str, Mapping[str, Any]],
    str | Mapping[str, Any] | Awaitable[str | Mapping[str, Any]],
]
PluginContextProviderHandler = Callable[
    [HookContext],
    str | list[str] | HookResult | Awaitable[str | list[str] | HookResult | None] | None,
]


@dataclass(frozen=True)
class PluginCommand:
    name: str
    handler: PluginCommandHandler
    description: str = ""
    plugin_id: str = ""
    source: PluginSource = "third_party"


@dataclass(frozen=True)
class PluginContextProvider:
    provider_id: str
    handler: PluginContextProviderHandler
    priority: int = 100


class PluginCommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, PluginCommand] = {}

    def register(self, command: PluginCommand) -> None:
        if not command.name.startswith("/"):
            raise ValueError(f"plugin command must start with '/': {command.name}")
        if command.name in self._commands:
            raise ValueError(f"plugin command already registered: {command.name}")
        self._commands[command.name] = command

    def get(self, name: str) -> PluginCommand:
        try:
            return self._commands[name]
        except KeyError as exc:
            raise KeyError(f"unknown plugin command: {name}") from exc

    def list(self) -> list[PluginCommand]:
        return [self._commands[name] for name in sorted(self._commands)]

    async def execute(
        self,
        name: str,
        argument: str = "",
        *,
        context: Mapping[str, Any] | None = None,
    ) -> str | Mapping[str, Any]:
        command = self.get(name)
        result = command.handler(argument, context or {})
        if inspect.isawaitable(result):
            return await result
        return result


def context_provider_hook(provider: PluginContextProvider):
    async def _handler(context: HookContext) -> HookResult:
        result = provider.handler(context)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return HookResult()
        if isinstance(result, HookResult):
            return result
        if isinstance(result, str):
            return HookResult(added_context=[result])
        return HookResult(added_context=list(result))

    return _handler
