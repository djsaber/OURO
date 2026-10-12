"""Named service calls for a Python worker, using only the public stdio protocol."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast
from uuid import uuid4

from .services import Registration
from .state import JsonValue
from .worker import WorkerPeer


class _RemoteRegistration:
    def __init__(self, owner: WorkerServices, identity: str, handler_id: str) -> None:
        self.owner = owner
        self.identity = identity
        self.handler_id = handler_id
        self.closed = False

    async def close(self) -> None:
        if self.closed:
            return
        # Disable local entry before asking the provider to remove its reference.
        self.owner.handlers.pop(self.handler_id, None)
        await self.owner.peer.close_registration(self.identity)
        self.closed = True


class WorkerServices:
    """Use as PluginServices; register invoke_handler as the handlers.invoke route."""

    def __init__(self, peer: WorkerPeer) -> None:
        self.peer = peer
        self.handlers: dict[str, Callable[[JsonValue], JsonValue | Awaitable[JsonValue]]] = {}

    async def call(
        self, plugin_id: str, service: str, method: str, arguments: JsonValue = None
    ) -> JsonValue:
        return cast(
            JsonValue,
            await self.peer.request(
                "services.call",
                {
                    "plugin_id": plugin_id,
                    "service": service,
                    "method": method,
                    "arguments": arguments,
                },
                timeout=0,
            ),
        )

    async def register(
        self,
        plugin_id: str,
        service: str,
        method: str,
        handler: Callable[[JsonValue], JsonValue | Awaitable[JsonValue]],
        arguments: JsonValue = None,
    ) -> Registration:
        handler_id, identity = uuid4().hex, uuid4().hex
        registration = _RemoteRegistration(self, identity, handler_id)
        self.handlers[handler_id] = handler
        try:
            await self.peer.request(
                "services.register",
                {
                    "plugin_id": plugin_id,
                    "service": service,
                    "method": method,
                    "arguments": arguments,
                    "handler_id": handler_id,
                    "registration_id": identity,
                },
            )
        except BaseException:
            await registration.close()
            raise
        return registration

    async def invoke_handler(self, params: Mapping[str, Any]) -> JsonValue:
        handler = self.handlers[params["handler_id"]]
        if inspect.iscoroutinefunction(handler):
            return await handler(params.get("arguments"))
        task = asyncio.create_task(asyncio.to_thread(handler, params.get("arguments")))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            result = task.result()
            if inspect.iscoroutine(result):
                result.close()
            raise
        return await result if inspect.isawaitable(result) else result
