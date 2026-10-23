from __future__ import annotations

import asyncio

import pytest

from ouro.application.tasks.scheduler import TaskScheduler
from ouro.automation.local_files import LocalTaskRepository
from ouro.sdk.plugins import v5 as sdk
from tests.application.test_tasks import task_runtime
from tests.plugins.test_lifecycle import PLUGIN_ID, LifecyclePlugin, create_runtime


class ScopedServices(LifecyclePlugin):
    def __init__(self, context):
        super().__init__(context)
        self.events = []
        self.home_started = asyncio.Event()
        self.fail_stop = False

    async def prepare(self):
        async def start_home():
            self.events.append("home:start")
            self.home_started.set()

        async def stop_home():
            self.events.append("home:stop")
            if self.fail_stop:
                raise RuntimeError("home service still owns its resource")
            self.home_started.clear()

        return sdk.ContributionSet(
            background_services=[
                sdk.BackgroundServiceContribution(
                    "local",
                    lambda: self.events.append("host:start"),
                    lambda: self.events.append("host:stop"),
                ),
                sdk.BackgroundServiceContribution("shared", start_home, stop_home),
            ]
        )


def scoped_runtime(path, instances):
    def create(context):
        instance = ScopedServices(context)
        instances.append(instance)
        return instance

    return create_runtime(
        path,
        create,
        contributes={
            "background_services": [
                {"id": "local"},
                {"id": "shared", "scope": "home"},
            ]
        },
    )


async def test_home_services_follow_owner_without_stopping_host_services(tmp_path):
    instances = []
    runtime = scoped_runtime(tmp_path, instances)
    try:
        assert await runtime.enable(PLUGIN_ID) == "active"
        first = instances[0]
        assert first.events == ["host:start"]
        await runtime.start_home_services()
        await runtime.start_home_services()
        assert first.events == ["host:start", "home:start"]
        await runtime.stop_home_services()
        await runtime.stop_home_services()
        assert first.events == ["host:start", "home:start", "home:stop"]
        await runtime.start_home_services()
        assert await runtime.disable(PLUGIN_ID) == "disabled"
        assert first.events[-3:] == ["home:start", "home:stop", "host:stop"]
        assert not runtime.has_resources(PLUGIN_ID)
    finally:
        await runtime.close()


async def test_home_ownership_applies_to_new_generations_and_upgrade_handover(tmp_path):
    instances = []
    runtime = scoped_runtime(tmp_path, instances)
    try:
        await runtime.start_home_services()
        assert await runtime.enable(PLUGIN_ID) == "active"
        assert instances[0].events == ["host:start", "home:start"]
        assert await runtime.restart(PLUGIN_ID) == "active"
        assert instances[0].events[-2:] == ["home:stop", "host:stop"]
        assert instances[1].events == ["host:start", "home:start"]
        await runtime.stop_home_services()
        assert instances[1].events[-1] == "home:stop"
    finally:
        await runtime.close()


@pytest.mark.parametrize("task_backend", ["files", "sqlite"])
async def test_scheduler_keeps_home_lock_until_plugin_service_cleanup_succeeds(
    tmp_path, task_backend
):
    instances = []
    runtime = scoped_runtime(tmp_path, instances)
    peer = LocalTaskRepository(tmp_path / "home")
    scheduler = None
    try:
        await runtime.enable(PLUGIN_ID)
        async with task_runtime(tmp_path / "home", task_backend=task_backend) as (
            tasks,
            execution,
            _,
            _,
        ):
            scheduler = TaskScheduler(
                tasks, execution, refresh_seconds=0.01, owned_services=runtime
            )
            await scheduler.start()
            instance = instances[0]
            async with asyncio.timeout(5):
                await instance.home_started.wait()
            assert not peer.acquire_scheduler()
            instance.fail_stop = True
            with pytest.raises(ExceptionGroup, match="have not stopped"):
                await scheduler.close()
            assert not peer.acquire_scheduler()
            instance.fail_stop = False
            await scheduler.close()
            assert peer.acquire_scheduler()
            assert "host:stop" not in instance.events
    finally:
        for instance in instances:
            instance.fail_stop = False
        if scheduler is not None:
            await scheduler.close()
        peer.release_scheduler()
        await runtime.close()


async def test_scheduler_nonowner_does_not_start_home_services(tmp_path):
    instances = []
    runtime = scoped_runtime(tmp_path, instances)
    peer = LocalTaskRepository(tmp_path / "home")
    scheduler = None
    try:
        await runtime.enable(PLUGIN_ID)
        assert peer.acquire_scheduler()
        async with task_runtime(tmp_path / "home") as (tasks, execution, _, _):
            checked = asyncio.Event()

            async def refresh():
                checked.set()

            scheduler = TaskScheduler(
                tasks,
                execution,
                refresh_seconds=0.01,
                owned_services=runtime,
                refresh_environment=refresh,
            )
            await scheduler.start()
            async with asyncio.timeout(5):
                await checked.wait()
            assert instances[0].events == ["host:start"]
            peer.release_scheduler()
            tasks.changed.set()
            async with asyncio.timeout(5):
                await instances[0].home_started.wait()
            await scheduler.close()
            assert instances[0].events == ["host:start", "home:start", "home:stop"]
    finally:
        if scheduler is not None:
            await scheduler.close()
        peer.release_scheduler()
        await runtime.close()
