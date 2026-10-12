from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from .scheduled import ScheduledInvocation
from .state import JsonValue


class ServiceHandler(Protocol):
    async def __call__(self, arguments: JsonValue = None) -> JsonValue: ...


class Registration(Protocol):
    async def close(self) -> None: ...


@dataclass(frozen=True)
class ServiceCall:
    caller_id: str
    arguments: JsonValue = None
    handler: ServiceHandler | None = None
    on_close: Callable[[Callable[[], None | Awaitable[None]]], None] | None = None
    scheduled: ScheduledInvocation | None = None


type ServiceMethod = Callable[[ServiceCall], JsonValue | Awaitable[JsonValue]]


@dataclass(frozen=True)
class ServiceContribution:
    id: str
    methods: Mapping[str, ServiceMethod] = field(default_factory=dict)


class PluginServices(Protocol):
    async def call(
        self, plugin_id: str, service: str, method: str, arguments: JsonValue = None
    ) -> JsonValue: ...

    async def register(
        self,
        plugin_id: str,
        service: str,
        method: str,
        handler: Callable[[JsonValue], JsonValue | Awaitable[JsonValue]],
        arguments: JsonValue = None,
    ) -> Registration: ...
