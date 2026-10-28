from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from ouro.application.plugins.service import PluginService
from ouro.core.observers import ContextPreparedObservation, ObserverRegistry
from ouro.plugins.builtin import BuiltinPluginDefinition, BuiltinPluginRegistry
from ouro.plugins.contributions.preparation import ContributionCoordinator
from ouro.plugins.installation.store import PluginPackageStore
from ouro.plugins.lifecycle.runtime import PluginRuntimeResource
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.registry import PluginCommandError, PluginCommandRegistry
from ouro.plugins.resources import OwnedTaskGroup
from ouro.sdk.plugins import v5 as sdk
from ouro.tools import ToolRegistry

PLUGIN_ID = "dev.ouro.lifecycle"


class LifecyclePlugin:
    def __init__(self, context: sdk.PluginContext) -> None:
        self.context = context

    async def prepare(self) -> sdk.ContributionSet:
        return sdk.ContributionSet()

    async def start(self) -> None:
        pass

    async def stop(self, reason: sdk.StopReason, deadline: float) -> None:
        pass


def create_runtime(
    tmp_path: Path,
    factory: Callable[[sdk.PluginContext], LifecyclePlugin],
    *,
    contributes: dict[str, object] | None = None,
) -> PluginRuntimeResource:
    definition = BuiltinPluginDefinition(
        manifest=PluginManifest.model_validate(
            {
                "schema_version": 3,
                "plugin_id": PLUGIN_ID,
                "name": "Lifecycle",
                "version": "1.0.0",
                "namespace": "lifecycle",
                "engines": {"ouro": ">=0.3.0,<0.4.0", "plugin_api": "^5.0.0"},
                "runtime": {
                    "kind": "python-inprocess",
                    "entrypoint": "lifecycle_plugin:create_plugin",
                },
                "contributes": contributes or {},
            }
        ),
        factory=factory,
        root=tmp_path,
        default_enabled=False,
    )
    return PluginRuntimeResource(
        PluginPackageStore(tmp_path / "home"),
        ContributionCoordinator(
            tools=ToolRegistry(), observers=ObserverRegistry(), commands=PluginCommandRegistry()
        ),
        builtin_plugins=BuiltinPluginRegistry((definition,)),
        stop_timeout_seconds=0.2,
    )


@pytest.mark.parametrize("phase", ["prepare", "start"])
async def test_candidate_rejects_private_data_writes_before_publication(tmp_path, phase):
    class Writer(LifecyclePlugin):
        async def prepare(self):
            if phase == "prepare":
                self.context.data.write_bytes("state.txt", b"candidate")
            return await super().prepare()

        async def start(self):
            if phase == "start":
                self.context.data.write_bytes("state.txt", b"candidate")

    runtime = create_runtime(tmp_path, Writer)
    state = runtime.store.data_root / PLUGIN_ID / "state.txt"
    state.parent.mkdir(parents=True)
    state.write_bytes(b"committed")
    try:
        assert await runtime.enable(PLUGIN_ID) == "failed"
        assert state.read_bytes() == b"committed"
        with runtime.snapshot() as catalog:
            assert catalog.generations == ()
        assert any("data" in item.message.lower() for item in runtime.diagnostics(PLUGIN_ID))
    finally:
        await runtime.close()


async def test_failed_service_stop_blocks_replacement_until_cleanup_succeeds(tmp_path):
    events: list[str] = []
    instances: list[ServicePlugin] = []

    class ServicePlugin(LifecyclePlugin):
        fail_stop = False

        def __init__(self, context):
            super().__init__(context)
            self.number = len(instances)
            instances.append(self)

        async def prepare(self):
            async def start_service():
                events.append(f"start:{self.number}")

            async def stop_service():
                events.append(f"stop:{self.number}")
                if self.fail_stop:
                    raise RuntimeError("service still owns its resource")

            return sdk.ContributionSet(
                background_services=[
                    sdk.BackgroundServiceContribution("watcher", start_service, stop_service)
                ]
            )

    runtime = create_runtime(
        tmp_path, ServicePlugin, contributes={"background_services": [{"id": "watcher"}]}
    )
    try:
        assert await runtime.enable(PLUGIN_ID) == "active"
        instances[0].fail_stop = True
        assert await runtime.restart(PLUGIN_ID) in {"degraded", "failed"}
        assert not any(item.startswith("start:") and item != "start:0" for item in events)
        assert await runtime.restart(PLUGIN_ID) in {"degraded", "failed"}
        assert not any(item.startswith("start:") and item != "start:0" for item in events)
    finally:
        instances[0].fail_stop = False
        await runtime.close()


async def test_failed_runtime_shutdown_can_retry_its_retained_generation(tmp_path):
    class RetryStop(LifecyclePlugin):
        attempts = 0

        async def stop(self, reason, deadline):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("resource has not stopped")

    runtime = create_runtime(tmp_path, RetryStop)
    await runtime.enable(PLUGIN_ID)
    with pytest.raises(ExceptionGroup, match="shutdown failed"):
        await runtime.close()
    assert runtime.has_resources(PLUGIN_ID)
    await runtime.close()
    assert not runtime.has_resources(PLUGIN_ID)


async def test_disable_reports_stopping_until_instance_cleanup_finishes(tmp_path):
    stopping = asyncio.Event()
    finish = asyncio.Event()

    class SlowStop(LifecyclePlugin):
        async def stop(self, reason, deadline):
            stopping.set()
            await finish.wait()

    runtime = create_runtime(tmp_path, SlowStop)
    await runtime.enable(PLUGIN_ID)
    disabling = asyncio.create_task(runtime.disable(PLUGIN_ID))
    try:
        await asyncio.wait_for(stopping.wait(), timeout=1)
        assert runtime.status(PLUGIN_ID) == "stopping"
        assert not disabling.done()
        finish.set()
        assert await disabling == "disabled"
        assert runtime.status(PLUGIN_ID) == "disabled"
    finally:
        finish.set()
        await disabling
        await runtime.close()


async def test_unpublished_task_group_closes_owned_coroutines():
    async def work():
        raise AssertionError("unpublished work must not run")

    coroutine = work()
    tasks = OwnedTaskGroup(active=False)
    tasks.start(coroutine)
    await tasks.close()
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED


async def test_task_group_timeout_keeps_ownership_until_the_task_settles():
    entered, cancelled, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await finish.wait()

    coroutine = work()
    tasks = OwnedTaskGroup()
    tasks.start(coroutine)
    await entered.wait()
    try:
        with pytest.raises(TimeoutError, match="have not stopped"):
            await tasks.close(timeout=0.01)
        assert cancelled.is_set()
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_SUSPENDED
    finally:
        finish.set()
        await tasks.close()
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED


async def test_nonblocking_disable_exposes_pending_background_cleanup(tmp_path):
    stopping, finish = asyncio.Event(), asyncio.Event()

    class SlowService(LifecyclePlugin):
        async def prepare(self):
            async def stop_service():
                stopping.set()
                await finish.wait()

            return sdk.ContributionSet(
                background_services=[
                    sdk.BackgroundServiceContribution("watcher", lambda: None, stop_service)
                ]
            )

    runtime = create_runtime(
        tmp_path, SlowService, contributes={"background_services": [{"id": "watcher"}]}
    )
    await runtime.enable(PLUGIN_ID)
    try:
        assert await runtime.disable(PLUGIN_ID, wait=False) == "stopping"
        await asyncio.wait_for(stopping.wait(), timeout=1)
        assert await runtime.wait_stopped(PLUGIN_ID) == "stopping"
        assert await runtime.enable(PLUGIN_ID) == "stopping"
        finish.set()
        assert await runtime.wait_stopped(PLUGIN_ID) == "disabled"
    finally:
        finish.set()
        await runtime.close()


async def test_cancelled_disable_does_not_abandon_cleanup(tmp_path):
    stopping, finish = asyncio.Event(), asyncio.Event()
    stopped = []

    class SlowStop(LifecyclePlugin):
        async def stop(self, reason, deadline):
            stopping.set()
            await finish.wait()
            stopped.append(reason)

    runtime = create_runtime(tmp_path, SlowStop)
    await runtime.enable(PLUGIN_ID)
    disabling = asyncio.create_task(runtime.disable(PLUGIN_ID))
    try:
        await asyncio.wait_for(stopping.wait(), timeout=1)
        disabling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await disabling
        assert runtime.status(PLUGIN_ID) == "stopping"
        finish.set()
        assert await runtime.wait_stopped(PLUGIN_ID) == "disabled"
        assert stopped == ["disable"]
    finally:
        finish.set()
        await runtime.close()


async def test_disable_revokes_every_retiring_generation(tmp_path):
    class Command(LifecyclePlugin):
        async def prepare(self):
            return sdk.ContributionSet(
                commands=[
                    sdk.CommandContribution(id="run", description="Run", handler=lambda _: "ok")
                ]
            )

    runtime = create_runtime(
        tmp_path,
        Command,
        contributes={"commands": [{"id": "run", "name": "run", "description": "Run"}]},
    )
    assert await runtime.enable(PLUGIN_ID) == "active", runtime.diagnostics()
    previous = runtime.snapshot()
    try:
        await runtime.restart(PLUGIN_ID)
        assert await runtime.disable(PLUGIN_ID) == "disabled"
        with pytest.raises(PluginCommandError, match="unavailable"):
            await previous.commands.execute(f"{PLUGIN_ID}:run", "")
    finally:
        previous.close()
        await runtime.close()


async def test_disable_keeps_sync_sdk_callback_owned_until_thread_finishes(tmp_path):
    entered, finish = threading.Event(), threading.Event()
    stopped = asyncio.Event()

    class Command(LifecyclePlugin):
        async def prepare(self):
            def run(call):
                entered.set()
                assert finish.wait(timeout=5)
                return "finished"

            return sdk.ContributionSet(
                commands=[sdk.CommandContribution(id="run", description="Run", handler=run)]
            )

        async def stop(self, reason, deadline):
            stopped.set()

    runtime = create_runtime(
        tmp_path,
        Command,
        contributes={"commands": [{"id": "run", "name": "run", "description": "Run"}]},
    )
    await runtime.enable(PLUGIN_ID)
    with runtime.catalog().bind_command(f"{PLUGIN_ID}:run") as binding:
        commands, _ = binding.value
        running = asyncio.create_task(commands.execute(f"{PLUGIN_ID}:run"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert await runtime.disable(PLUGIN_ID) == "stopping"
            assert not stopped.is_set()
            assert not running.done()
            finish.set()
            with pytest.raises(PluginCommandError, match="unavailable"):
                await running
            assert await runtime.wait_stopped(PLUGIN_ID) == "disabled"
            assert stopped.is_set()
        finally:
            finish.set()
            await asyncio.gather(running, return_exceptions=True)
            await runtime.close()


async def test_failed_candidate_cleanup_remains_owned_and_blocks_reactivation(tmp_path):
    instances = []

    class FailedStart(LifecyclePlugin):
        fail_stop = True

        def __init__(self, context):
            super().__init__(context)
            instances.append(self)

        async def start(self):
            raise RuntimeError("candidate cannot start")

        async def stop(self, reason, deadline):
            if self.fail_stop:
                raise RuntimeError("candidate still owns a resource")

    runtime = create_runtime(tmp_path, FailedStart)
    try:
        assert await runtime.enable(PLUGIN_ID) == "failed"
        assert any(item.code == "plugin_retirement_failed" for item in runtime.diagnostics())
        assert any(
            "candidate still owns a resource" in item.message for item in runtime.diagnostics()
        )
        assert await runtime.enable(PLUGIN_ID) == "failed"
        assert len(instances) == 1
        instances[0].fail_stop = False
        assert await runtime.disable(PLUGIN_ID) == "disabled"
    finally:
        for instance in instances:
            instance.fail_stop = False
        await runtime.close()


async def test_private_data_cannot_be_purged_while_cleanup_is_running(tmp_path):
    stopping, finish = asyncio.Event(), asyncio.Event()

    class SlowStop(LifecyclePlugin):
        async def stop(self, reason, deadline):
            stopping.set()
            await finish.wait()

    runtime = create_runtime(tmp_path, SlowStop)
    service = PluginService(runtime.store, runtime=runtime, builtin_plugins=runtime.builtin_plugins)
    state = runtime.store.data_root / PLUGIN_ID / "state.txt"
    state.parent.mkdir(parents=True)
    state.write_bytes(b"owned")
    await runtime.enable(PLUGIN_ID)
    try:
        assert await runtime.disable(PLUGIN_ID, wait=False) == "stopping"
        await stopping.wait()
        with pytest.raises(ValueError, match="resources"):
            service.purge_data(PLUGIN_ID)
        assert state.read_bytes() == b"owned"
        finish.set()
        assert await runtime.wait_stopped(PLUGIN_ID) == "disabled"
        assert service.purge_data(PLUGIN_ID).status == "disabled"
        assert not state.exists()
    finally:
        finish.set()
        await runtime.close()


async def test_old_decision_can_emit_observations_after_plugin_disable(tmp_path):
    class Observer(LifecyclePlugin):
        async def prepare(self):
            return sdk.ContributionSet(
                observers=[
                    sdk.ObserverContribution(
                        id="listen", event="context_prepared", handler=lambda _: None
                    )
                ]
            )

    runtime = create_runtime(
        tmp_path,
        Observer,
        contributes={"observers": [{"id": "listen", "event": "context_prepared"}]},
    )
    await runtime.enable(PLUGIN_ID)
    previous = runtime.snapshot()
    try:
        assert await runtime.disable(PLUGIN_ID) == "disabled"
        observation = ContextPreparedObservation(
            session_id="session", run_id="run", message_count=1, tool_count=0, model_round=0
        )
        with previous.catalog.bind_observers(
            observation, active_controller_ids=lambda: set()
        ) as binding:
            assert binding.value.matching(observation) == []
    finally:
        previous.close()
        await runtime.close()


async def test_shutdown_releases_resources_without_reading_persistent_state(tmp_path):
    stopped = []

    class Plugin(LifecyclePlugin):
        async def stop(self, reason, deadline):
            stopped.append(reason)

    runtime = create_runtime(tmp_path, Plugin)
    assert await runtime.enable(PLUGIN_ID) == "active"
    runtime.store.state_path.write_text("{invalid", encoding="utf-8")
    await runtime.close()
    assert stopped == ["shutdown"]
    assert not runtime.has_resources(PLUGIN_ID)
    assert runtime.store.state_path.read_text(encoding="utf-8") == "{invalid"


@pytest.mark.parametrize("suppress_cancel", [False, True])
async def test_disable_cancels_plugin_work_without_cancelling_the_host_caller(
    tmp_path, suppress_cancel
):
    entered = asyncio.Event()

    class Command(LifecyclePlugin):
        async def prepare(self):
            async def run(call):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    if not suppress_cancel:
                        raise
                return "late result"

            return sdk.ContributionSet(
                commands=[sdk.CommandContribution(id="run", description="Run", handler=run)]
            )

    runtime = create_runtime(
        tmp_path,
        Command,
        contributes={"commands": [{"id": "run", "name": "run", "description": "Run"}]},
    )
    await runtime.enable(PLUGIN_ID)

    async def host_call():
        with runtime.catalog().bind_command(f"{PLUGIN_ID}:run") as binding:
            commands, _ = binding.value
            with pytest.raises(PluginCommandError, match="unavailable"):
                await commands.execute(f"{PLUGIN_ID}:run")
        return asyncio.current_task().cancelling()

    caller = asyncio.create_task(host_call())
    try:
        await entered.wait()
        assert await runtime.disable(PLUGIN_ID) == "disabled"
        assert await caller == 0
    finally:
        await asyncio.gather(caller, return_exceptions=True)
        await runtime.close()
