from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, cast

from ouro.core.base import new_id
from ouro.plugins.manifest import ServiceExportSpec
from ouro.sdk.plugins.services import (
    PluginServices,
    Registration,
    ServiceCall,
    ServiceContribution,
    ServiceHandler,
    ServiceMethod,
)
from ouro.sdk.plugins.state import JsonValue


class WorkerRequests(Protocol):
    @property
    def is_healthy(self) -> bool: ...

    async def request(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout: float | None = None,
    ) -> object: ...


class WorkerServiceBridge:
    """Translate named service calls and owned handler identities across stdio."""

    def __init__(self, client: WorkerRequests, services: PluginServices) -> None:
        self.client = client
        self.services = services
        self._handlers: dict[str, ServiceHandler] = {}
        self._registrations: dict[str, Registration] = {}

    async def dispatch(self, method: str, params: Mapping[str, object]) -> object:
        if method == "services.call":
            return await self.services.call(
                _name(params, "plugin_id"),
                _name(params, "service"),
                _name(params, "method"),
                cast(JsonValue, params.get("arguments")),
            )
        if method == "services.register":
            handler_id = _name(params, "handler_id")
            identity = _name(params, "registration_id")
            if identity in self._registrations:
                raise ValueError("service registration identity is already in use")

            async def callback(arguments: JsonValue) -> JsonValue:
                return cast(
                    JsonValue,
                    await self.client.request(
                        "handlers.invoke",
                        {"handler_id": handler_id, "arguments": arguments},
                        timeout=0,
                    ),
                )

            registration = await self.services.register(
                _name(params, "plugin_id"),
                _name(params, "service"),
                _name(params, "method"),
                callback,
                cast(JsonValue, params.get("arguments")),
            )
            self._registrations[identity] = registration
            return None
        if method == "registrations.close":
            identity = _name(params, "registration_id")
            owned = self._registrations.get(identity)
            if owned is not None:
                await owned.close()
                del self._registrations[identity]
            return None
        if method == "handlers.invoke":
            handler = self._handlers[_name(params, "handler_id")]
            return await handler(cast(JsonValue, params.get("arguments")))
        raise ValueError(f"unknown Host service method: {method}")

    def contributions(self, declarations: list[ServiceExportSpec]) -> list[ServiceContribution]:
        return [
            ServiceContribution(
                item.id, {name: self._method(item.id, name) for name in item.methods}
            )
            for item in declarations
        ]

    def _method(self, service: str, method: str) -> ServiceMethod:
        async def invoke(call: ServiceCall) -> JsonValue:
            params: dict[str, object] = {
                "service": service,
                "method": method,
                "caller_id": call.caller_id,
                "arguments": call.arguments,
            }
            if call.scheduled is not None:
                params["scheduled"] = {
                    "task_id": call.scheduled.task_id,
                    "execution_id": call.scheduled.execution_id,
                    "scheduled_at": call.scheduled.scheduled_at.isoformat(),
                }
            if call.handler is None:
                return cast(
                    JsonValue, await self.client.request("services.call", params, timeout=0)
                )
            assert call.on_close is not None
            handler_id = new_id("handler")
            registration_id = new_id("registration")
            self._handlers[handler_id] = call.handler

            async def close() -> None:
                if self.client.is_healthy:
                    await self.client.request(
                        "registrations.close",
                        {
                            "registration_id": registration_id,
                        },
                    )
                self._handlers.pop(handler_id, None)

            # Identity and cleanup exist before the peer can install the handler.
            call.on_close(close)
            params.update(handler_id=handler_id, registration_id=registration_id)
            result = await self.client.request("services.register", params)
            if result is not None:
                raise TypeError("worker service registration must return null")
            return None

        return invoke


def _name(params: Mapping[str, object], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a nonempty string")
    return value
