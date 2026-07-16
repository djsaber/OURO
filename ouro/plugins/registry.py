from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.hooks import HookContext, HookResult, PluginSource
from ouro.core.redaction import redact_text

_COMMAND_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


class PluginCommandRequest(OuroModel):
    command_id: str
    argument: str = ""
    session_id: str | None = None
    workspace: str | None = None


class PluginCommandResult(OuroModel):
    command_id: str
    output: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class PluginCommandError(RuntimeError):
    pass


PluginCommandHandler = Callable[
    [PluginCommandRequest],
    str
    | Mapping[str, Any]
    | PluginCommandResult
    | Awaitable[str | Mapping[str, Any] | PluginCommandResult],
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
    timeout_seconds: float = 5.0

    @property
    def command_id(self) -> str:
        return f"{self.plugin_id}:{self.name}"


@dataclass(frozen=True)
class PluginContextProvider:
    provider_id: str
    handler: PluginContextProviderHandler
    priority: int = 100


class PluginCommandRegistry:
    """Host-neutral, namespaced command registry with a typed execution boundary."""

    def __init__(self) -> None:
        self._commands: dict[str, PluginCommand] = {}

    def register(self, command: PluginCommand) -> None:
        self.register_many((command,))

    def register_many(self, commands: tuple[PluginCommand, ...]) -> None:
        updated = dict(self._commands)
        for command in commands:
            if not command.plugin_id:
                raise ValueError("plugin command requires plugin_id")
            if _COMMAND_NAME.fullmatch(command.name) is None:
                raise ValueError(
                    "plugin command name must use lowercase letters, numbers, '_' or '-'"
                )
            if command.timeout_seconds <= 0:
                raise ValueError("plugin command timeout must be positive")
            if command.command_id in updated:
                raise ValueError(f"plugin command already registered: {command.command_id}")
            updated[command.command_id] = command
        self._commands = updated

    def get(self, command_id: str) -> PluginCommand:
        try:
            return self._commands[command_id]
        except KeyError as exc:
            raise KeyError(f"unknown plugin command: {command_id}") from exc

    def list(self) -> list[PluginCommand]:
        return [self._commands[name] for name in sorted(self._commands)]

    async def execute(
        self,
        command_id: str,
        argument: str = "",
        *,
        session_id: str | None = None,
        workspace: str | None = None,
    ) -> PluginCommandResult:
        command = self.get(command_id)
        request = PluginCommandRequest(
            command_id=command_id,
            argument=argument,
            session_id=session_id,
            workspace=workspace,
        )
        try:
            if inspect.iscoroutinefunction(command.handler):
                raw = await asyncio.wait_for(
                    command.handler(request),
                    timeout=command.timeout_seconds,
                )
            else:
                raw = await asyncio.wait_for(
                    asyncio.to_thread(command.handler, request),
                    timeout=command.timeout_seconds,
                )
                if inspect.isawaitable(raw):
                    raw = await asyncio.wait_for(raw, timeout=command.timeout_seconds)
        except TimeoutError as exc:
            raise PluginCommandError(
                f"plugin command timed out after {command.timeout_seconds:g} seconds: {command_id}"
            ) from exc
        except Exception as exc:
            raise PluginCommandError(
                f"plugin command failed ({type(exc).__name__}): {redact_text(str(exc))}"
            ) from exc
        if isinstance(raw, PluginCommandResult):
            if raw.command_id != command_id:
                return raw.model_copy(update={"command_id": command_id})
            return raw
        if isinstance(raw, str):
            return PluginCommandResult(command_id=command_id, output=raw)
        return PluginCommandResult(command_id=command_id, data=dict(raw))


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


def bind_plugin_command(
    command: PluginCommand,
    *,
    plugin_id: str,
    source: PluginSource,
    maximum_timeout_seconds: float,
) -> PluginCommand:
    return replace(
        command,
        plugin_id=plugin_id,
        source=source,
        timeout_seconds=min(command.timeout_seconds, maximum_timeout_seconds),
    )
