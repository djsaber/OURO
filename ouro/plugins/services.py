from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass

from jsonschema import Draft202012Validator
from pydantic import ConfigDict, TypeAdapter

from ouro.core.callbacks import invoke_owned_callback
from ouro.plugins.catalog.gates import GenerationGate, PluginGenerationUnavailableError
from ouro.plugins.invocation import current_invocation, plugin_invocation
from ouro.plugins.manifest import ServiceExportSpec, ServiceMethodSpec
from ouro.sdk.plugins.scheduled import ScheduledInvocation
from ouro.sdk.plugins.services import (
    Registration,
    ServiceCall,
    ServiceContribution,
    ServiceMethod,
)
from ouro.sdk.plugins.state import JsonValue

_JSON: TypeAdapter[JsonValue] = TypeAdapter(
    JsonValue, config=ConfigDict(allow_inf_nan=False, strict=True)
)


@dataclass(frozen=True)
class _Method:
    spec: ServiceMethodSpec
    handler: ServiceMethod
    validate: Draft202012Validator


class _BoundHandler:
    def __init__(
        self,
        owner: ServiceEndpoint,
        handler: Callable[[JsonValue], JsonValue | Awaitable[JsonValue]],
    ) -> None:
        self.owner: ServiceEndpoint | None = owner
        self.handler: Callable[[JsonValue], JsonValue | Awaitable[JsonValue]] | None = handler

    async def __call__(self, arguments: JsonValue = None) -> JsonValue:
        owner, handler = self.owner, self.handler
        if owner is None or handler is None:
            raise PluginGenerationUnavailableError("service registration is closed")
        with owner.bind():
            async with owner.gate.invocation():
                with plugin_invocation(owner.plugin_id, writable=True) as invocation:
                    value = _JSON.validate_python(arguments)
                    return _JSON.validate_python(
                        await invoke_owned_callback(handler, value, on_cancel=invocation.close)
                    )

    def close(self) -> None:
        self.owner = None
        self.handler = None


class _Registration:
    def __init__(
        self,
        source: ServiceEndpoint,
        target: ServiceEndpoint,
        callback: _BoundHandler,
    ) -> None:
        self.owners = {source, target}
        self.callback = callback
        self.removals: list[Callable[[], None | Awaitable[None]]] = []
        self.closed = False
        self.lock = asyncio.Lock()
        for owner in self.owners:
            owner.registrations.add(self)

    def on_close(self, cleanup: Callable[[], None | Awaitable[None]]) -> None:
        if self.closed:
            raise RuntimeError("service registration is closed")
        self.removals.append(cleanup)

    async def close(self) -> None:
        async with self.lock:
            if self.closed:
                return
            self.callback.close()
            for cleanup in reversed(tuple(self.removals)):
                await invoke_owned_callback(cleanup)
                self.removals.remove(cleanup)
            self.closed = True
            for owner in self.owners:
                owner.registrations.discard(self)
            self.owners.clear()


class ServiceEndpoint:
    """Bind exports to exact dependency generations and own two-sided registrations."""

    def __init__(
        self,
        plugin_id: str,
        *,
        gate: GenerationGate,
        bind: Callable[[], AbstractContextManager[object]],
        dependencies: Mapping[str, ServiceEndpoint],
    ) -> None:
        self.plugin_id = plugin_id
        self.gate = gate
        self.bind = bind
        self.dependencies = dict(dependencies)
        self.methods: dict[tuple[str, str], _Method] = {}
        self.registrations: set[_Registration] = set()

    def prepare(
        self,
        declarations: Sequence[ServiceExportSpec],
        contributions: Sequence[ServiceContribution],
    ) -> None:
        supplied = {item.id: item for item in contributions}
        if len(supplied) != len(contributions) or set(supplied) != {s.id for s in declarations}:
            raise ValueError("service exports must match the manifest exactly")
        for declaration in declarations:
            implementation = supplied[declaration.id]
            if set(implementation.methods) != set(declaration.methods):
                raise ValueError(f"service methods must match the manifest: {declaration.id}")
            for name, spec in declaration.methods.items():
                handler = implementation.methods[name]
                if not callable(handler):
                    raise TypeError(f"service method is not callable: {declaration.id}/{name}")
                self.methods[declaration.id, name] = _Method(
                    spec, handler, Draft202012Validator(spec.input_schema)
                )

    def _target(self, plugin_id: str) -> ServiceEndpoint:
        if plugin_id == self.plugin_id:
            return self
        try:
            return self.dependencies[plugin_id]
        except KeyError as exc:
            raise PermissionError(
                f"service provider is not a declared dependency: {plugin_id}"
            ) from exc

    async def _invoke(
        self,
        target: ServiceEndpoint,
        service: str,
        method: str,
        arguments: JsonValue,
        registration: _Registration | None = None,
        scheduled: ScheduledInvocation | None = None,
    ) -> JsonValue:
        definition = target.methods.get((service, method))
        if definition is None:
            raise KeyError(f"service method is unavailable: {target.plugin_id}/{service}/{method}")
        if definition.spec.registration != (registration is not None):
            raise ValueError("service method requires the matching call or register operation")
        parent = current_invocation.get()
        if parent is not None:
            parent.require_active()
            if not parent.writable and (
                definition.spec.effect != "read_only" or registration is not None
            ):
                raise PermissionError("a read-only callback cannot mutate a plugin service")
        value = _JSON.validate_python(arguments)
        definition.validate.validate(value)
        async with self.gate.invocation():
            with target.bind():
                async with target.gate.invocation():
                    with plugin_invocation(
                        target.plugin_id, writable=definition.spec.effect != "read_only"
                    ) as invocation:
                        return await invoke_owned_callback(
                            definition.handler,
                            ServiceCall(
                                "ouro.tasks" if scheduled is not None else self.plugin_id,
                                value,
                                registration.callback if registration is not None else None,
                                registration.on_close if registration is not None else None,
                                scheduled,
                            ),
                            on_cancel=invocation.close,
                        )

    async def call(
        self, plugin_id: str, service: str, method: str, arguments: JsonValue = None
    ) -> JsonValue:
        result = await self._invoke(self._target(plugin_id), service, method, arguments)
        return _JSON.validate_python(result)

    def validate_scheduled(self, service: str, method: str, arguments: JsonValue) -> JsonValue:
        definition = self.methods.get((service, method))
        if definition is None or not definition.spec.schedulable:
            raise ValueError(
                f"Service method is not schedulable: {self.plugin_id}/{service}/{method}"
            )
        value = _JSON.validate_python(arguments)
        definition.validate.validate(value)
        return value

    async def call_scheduled(
        self, service: str, method: str, arguments: JsonValue, scheduled: ScheduledInvocation
    ) -> JsonValue:
        value = self.validate_scheduled(service, method, arguments)
        return _JSON.validate_python(
            await self._invoke(self, service, method, value, scheduled=scheduled)
        )

    async def register(
        self,
        plugin_id: str,
        service: str,
        method: str,
        handler: Callable[[JsonValue], JsonValue | Awaitable[JsonValue]],
        arguments: JsonValue = None,
    ) -> Registration:
        target = self._target(plugin_id)
        callback = _BoundHandler(self, handler)
        registration = _Registration(self, target, callback)
        try:
            result = await self._invoke(target, service, method, arguments, registration)
            if result is not None:
                raise TypeError("a registration method must return None")
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError
            return registration
        except BaseException:
            callback.close()
            await registration.close()
            raise

    async def close(self) -> None:
        errors: list[Exception] = []
        for registration in tuple(self.registrations):
            try:
                await registration.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("plugin service registrations did not close", errors)
        self.methods.clear()
        self.dependencies.clear()
