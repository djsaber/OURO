from __future__ import annotations

import asyncio
import threading

import pytest
from jsonschema import ValidationError

from ouro.core.callbacks import CallbackCancelled
from ouro.core.observers import ObserverRegistry
from ouro.plugins.builtin import BuiltinPluginDefinition, BuiltinPluginRegistry
from ouro.plugins.catalog.gates import PluginGenerationUnavailableError
from ouro.plugins.contributions.preparation import ContributionCoordinator
from ouro.plugins.installation.store import PluginPackageStore
from ouro.plugins.invocation import plugin_invocation
from ouro.plugins.lifecycle.runtime import PluginRuntimeResource
from ouro.plugins.manifest import PluginManifest
from ouro.sdk.plugins import v5 as sdk
from ouro.sdk.plugins.services import ServiceContribution
from ouro.tools import ToolRegistry

MEMORY = "dev.example.memory"
CONSUMER = "dev.example.consumer"
UNRELATED = "dev.example.unrelated"


class Memory:
    def __init__(self, context, instances):
        self.context = context
        self.filters = []
        self.label = f"generation-{len(instances)}"
        self.registered = asyncio.Event()
        self.registration_wait = None
        self.fail_first_cleanup = False
        self.cleanup_attempts = 0
        instances.append(self)

    async def search(self, call):
        if call.arguments == "write-in-read":
            self.context.data.write_bytes("forbidden.txt", b"write")
        value = self.label + ":" + call.arguments
        for transform in tuple(self.filters):
            value = await transform(value)
        return value

    def write(self, call):
        self.context.data.write_bytes("memory.txt", call.arguments.encode())

    async def add_filter(self, call):
        assert call.handler is not None and call.on_close is not None

        def remove():
            self.cleanup_attempts += 1
            if self.fail_first_cleanup and self.cleanup_attempts == 1:
                raise OSError("filter removal failed")
            self.filters.remove(call.handler)

        call.on_close(remove)
        self.filters.append(call.handler)
        self.registered.set()
        if self.registration_wait is not None:
            await self.registration_wait.wait()

    async def prepare(self):
        return sdk.ContributionSet(
            services=[
                ServiceContribution(
                    "memory",
                    {"search": self.search, "write": self.write, "filter": self.add_filter},
                )
            ]
        )

    async def start(self):
        pass

    async def stop(self, reason, deadline):
        pass


class Consumer:
    def __init__(self, context, instances):
        self.context = context
        instances.append(self)

    async def prepare(self):
        return sdk.ContributionSet()

    async def start(self):
        pass

    async def stop(self, reason, deadline):
        pass


@pytest.fixture
async def service_runtime(tmp_path):
    memories, consumers, unrelated = [], [], []

    def definition(plugin_id, factory, *, dependencies=(), exports=()):
        namespace = plugin_id.rsplit(".", 1)[1]
        return BuiltinPluginDefinition(
            manifest=PluginManifest.model_validate(
                {
                    "plugin_id": plugin_id,
                    "namespace": namespace,
                    "name": namespace,
                    "version": "1.0.0",
                    "engines": {"ouro": "^0.3.0", "plugin_api": "^5.0.0"},
                    "runtime": {
                        "kind": "python-inprocess",
                        "entrypoint": f"{namespace}_plugin:create",
                    },
                    "dependencies": {"plugins": list(dependencies)},
                    "exports": list(exports),
                }
            ),
            factory=factory,
            root=tmp_path,
            default_enabled=False,
        )

    registry = BuiltinPluginRegistry(
        (
            definition(
                MEMORY,
                lambda ctx: Memory(ctx, memories),
                exports=[
                    {
                        "id": "memory",
                        "methods": {
                            "search": {"effect": "read_only", "input_schema": {"type": "string"}},
                            "write": {
                                "effect": "state_changing",
                                "input_schema": {"type": "string"},
                            },
                            "filter": {"effect": "state_changing", "registration": True},
                        },
                    }
                ],
            ),
            definition(
                CONSUMER,
                lambda ctx: Consumer(ctx, consumers),
                dependencies=[
                    {
                        "plugin_id": MEMORY,
                        "version": "^1.0.0",
                    }
                ],
            ),
            definition(UNRELATED, lambda ctx: Consumer(ctx, unrelated)),
        )
    )
    runtime = PluginRuntimeResource(
        PluginPackageStore(tmp_path / "home"),
        ContributionCoordinator(tools=ToolRegistry(), observers=ObserverRegistry()),
        builtin_plugins=registry,
    )
    try:
        assert await runtime.enable(CONSUMER) == "active"
        assert await runtime.enable(UNRELATED) == "active"
        yield runtime, memories, consumers, unrelated
    finally:
        await runtime.close()


async def test_services_share_data_through_declared_owners(service_runtime):
    runtime, memories, consumers, unrelated = service_runtime
    services = consumers[-1].context.services
    assert await services.call(MEMORY, "memory", "search", "query") == "generation-0:query"
    await services.call(MEMORY, "memory", "write", "stored")
    assert memories[-1].context.data.read_bytes("memory.txt") == b"stored"
    with pytest.raises(ValidationError):
        await services.call(MEMORY, "memory", "search", {"not": "text"})
    with pytest.raises(PermissionError, match="declared dependency"):
        await unrelated[-1].context.services.call(MEMORY, "memory", "search", "query")


async def test_read_only_callbacks_cannot_mutate_services_or_register_handlers(service_runtime):
    _, _, consumers, _ = service_runtime
    services = consumers[-1].context.services
    with plugin_invocation(CONSUMER, writable=False):
        assert await services.call(MEMORY, "memory", "search", "read") == "generation-0:read"
        with pytest.raises(PermissionError, match="read-only"):
            await services.call(MEMORY, "memory", "write", "forbidden")
        with pytest.raises(PermissionError, match="read-only"):
            await services.register(MEMORY, "memory", "filter", lambda value: value)
    with pytest.raises(PermissionError, match="read-only"):
        await services.call(MEMORY, "memory", "search", "write-in-read")


async def test_registration_is_removed_when_consumer_stops(service_runtime):
    runtime, memories, consumers, _ = service_runtime
    services = consumers[-1].context.services
    registration = await services.register(MEMORY, "memory", "filter", lambda value: value.upper())
    callback = memories[-1].filters[0]
    assert await services.call(MEMORY, "memory", "search", "test") == "GENERATION-0:TEST"
    assert await runtime.disable(CONSUMER) == "disabled"
    assert memories[-1].filters == []
    with pytest.raises(PluginGenerationUnavailableError):
        await callback("late")
    await registration.close()
    await registration.close()


async def test_cancelled_registration_releases_provider_state(service_runtime):
    _, memories, consumers, _ = service_runtime
    memory = memories[-1]
    memory.registration_wait = asyncio.Event()
    pending = asyncio.create_task(
        consumers[-1].context.services.register(MEMORY, "memory", "filter", lambda value: value)
    )
    await memory.registered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert memory.filters == []


async def test_service_dependency_stays_bound_to_its_generation_during_upgrade(service_runtime):
    runtime, _, consumers, _ = service_runtime
    old = consumers[-1].context.services
    lease = runtime.snapshot()
    try:
        assert await runtime.restart(MEMORY) == "active"
        current = consumers[-1].context.services
        assert await old.call(MEMORY, "memory", "search", "old") == "generation-0:old"
        assert await current.call(MEMORY, "memory", "search", "new") == "generation-1:new"
    finally:
        lease.close()


async def test_cancelled_sync_service_callback_cannot_start_late_sdk_operations(service_runtime):
    _, memories, consumers, _ = service_runtime
    services = consumers[-1].context.services
    started, release = threading.Event(), threading.Event()
    failures = []

    def slow_handler(value):
        started.set()
        release.wait()
        try:
            asyncio.run(services.call(MEMORY, "memory", "search", "late"))
        except RuntimeError as exc:
            failures.append(str(exc))
        return value

    registration = await services.register(MEMORY, "memory", "filter", slow_handler)
    pending = asyncio.create_task(services.call(MEMORY, "memory", "search", "start"))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(CallbackCancelled):
            await pending
        assert failures == ["plugin invocation has finished"]
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await registration.close()
    assert not memories[-1].filters


async def test_registration_cleanup_failure_retains_owner_until_explicit_retry(service_runtime):
    runtime, memories, consumers, _ = service_runtime
    services = consumers[-1].context.services
    memories[-1].fail_first_cleanup = True
    await services.register(MEMORY, "memory", "filter", lambda value: value)
    assert await runtime.disable(CONSUMER) == "failed"
    assert runtime.has_resources(CONSUMER)
    assert memories[-1].filters
    assert await runtime.disable(CONSUMER) == "disabled"
    assert not runtime.has_resources(CONSUMER)
    assert not memories[-1].filters
