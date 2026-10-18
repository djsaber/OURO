from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ouro.application.plugins import PluginService
from ouro.application.runtime import RuntimeService
from ouro.application.supervisor import RuntimeSupervisor
from ouro.application.task_execution import TaskExecutionCoordinator
from ouro.application.task_scheduler import TaskScheduler
from ouro.application.tasks import TaskService
from ouro.automation.local_files import LocalTaskRepository
from ouro.automation.sqlite import SQLiteTaskRepository
from ouro.core.adapter import RuntimeAdapter
from ouro.core.observers import ObserverRegistry
from ouro.core.provider import ModelMetadata
from ouro.core.tasks import (
    AgentTaskTarget,
    PluginTaskCall,
    PluginTaskTarget,
    TaskConfiguration,
    TaskSchedule,
)
from ouro.plugins.contributions import ContributionCoordinator
from ouro.plugins.runtime import PluginRuntimeResource
from ouro.plugins.store import PluginPackageStore
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry

PLUGIN = "dev.example.scheduled"
NOW = datetime(2026, 10, 12, 9, tzinfo=UTC)
PROBE = Path(__file__).parents[1] / "plugins/fixtures/scheduled_service.py"


def package(
    root, kind, *, label="one", version="1.0.0", schedulable=True, permissions=True, sessions=False
):
    source = root / version
    (source / ".ouro-plugin").mkdir(parents=True)
    (source / "scheduled_plugin.py").write_text(
        PROBE.read_text(encoding="utf-8").replace('LABEL = "one"', f'LABEL = "{label}"'),
        encoding="utf-8",
    )
    manifest = {
        "plugin_id": PLUGIN,
        "namespace": "scheduled",
        "name": "Scheduled probe",
        "version": version,
        "engines": {"ouro": "^0.3.0", "plugin_api": "^5.0.0"},
        "runtime": {"kind": kind, "entrypoint": "scheduled_plugin:create_plugin"}
        if kind == "python-inprocess"
        else {"kind": "worker", "command": ["{python}", "scheduled_plugin.py"]},
        "permissions": [{"id": "background.run", "reason": "Run scheduled synchronization"}]
        if permissions
        else [],
        "exports": [
            {
                "id": "sync",
                "methods": {
                    "run": {
                        "effect": "state_changing",
                        "schedulable": schedulable,
                        "input_schema": {"type": "object"},
                    },
                    "inspect": {"effect": "read_only"},
                    "release": {"effect": "state_changing"},
                    "schedule": {"effect": "state_changing"},
                    "read_schedule": {"effect": "read_only"},
                },
            }
        ],
    }
    if sessions:
        manifest["permissions"].append({"id": "sessions.write", "reason": "Schedule Agent work"})
    (source / ".ouro-plugin/plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return source


@pytest.fixture(params=["python-inprocess", "worker"])
def kind(request):
    return request.param


@pytest.fixture(params=[LocalTaskRepository, SQLiteTaskRepository])
async def host(tmp_path, request, kind):
    tools = ToolRegistry()
    store = LocalSessionStore(tmp_path / "home")
    store.initialize()
    runtime = RuntimeService(
        store=store,
        tools=tools,
        adapter=RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="task", provider_id="faux")),
    )
    plugins = PluginRuntimeResource(
        PluginPackageStore(tmp_path / "home"),
        ContributionCoordinator(tools=tools, observers=ObserverRegistry()),
    )
    service = PluginService(plugins.store, runtime=plugins)
    service.install(package(tmp_path, kind))
    repository = request.param(tmp_path / "home")
    repository.initialize()
    clock = [NOW]
    tasks = TaskService(repository, runtime, clock=lambda: clock[0], plugin_tasks=plugins)
    plugins.bind_task_service(tasks)
    assert (await service.enable(PLUGIN)).status == "active", service.diagnostics()
    supervisor = RuntimeSupervisor(runtime)
    await supervisor.start()
    execution = TaskExecutionCoordinator(tasks, supervisor)
    try:
        yield tasks, execution, plugins, service, clock
    finally:
        await supervisor.shutdown()
        await execution.close()
        await plugins.close()


def configuration(**arguments):
    return TaskConfiguration(
        target=PluginTaskTarget(
            plugin_id=PLUGIN, service="sync", method="run", arguments=arguments
        ),
        schedule=TaskSchedule(frequency="hourly", starts_at=NOW),
    )


async def settle(tasks, execution, task_id):
    async with asyncio.timeout(10):
        while True:
            await execution.reconcile()
            entries = await tasks.executions(task_id)
            if entries and entries[0].ended_at is not None:
                return entries[0]
            await asyncio.sleep(0.01)


async def started(endpoint):
    async with asyncio.timeout(5):
        while not await endpoint.call(PLUGIN, "sync", "inspect"):
            await asyncio.sleep(0.01)


async def test_scheduled_service_persists_exact_target_and_result_without_a_session(host):
    tasks, execution, plugins, _, _ = host
    task = await tasks.create(configuration(value="payload"))
    await execution.tick()
    receipt = await settle(tasks, execution, task.task_id)
    assert receipt.status == "completed", receipt.error
    assert isinstance(receipt.target, PluginTaskCall)
    assert receipt.target.implementation.release_id == plugins._active[PLUGIN].record.release_id
    assert receipt.result == {
        "label": "one",
        "calls": 1,
        "task_id": task.task_id,
        "execution_id": receipt.execution_id,
        "scheduled_at": NOW.isoformat(),
        "arguments": {"value": "payload"},
        "caller_id": "ouro.tasks",
    }
    assert not tasks.runtime.sessions.catalog().entries
    assert (await tasks.get(task.task_id)).session_id is None
    assert plugins._active[PLUGIN].lease_count == 0
    assert not execution._plugin_jobs


async def test_disabled_provider_pauses_future_plan_without_claim_or_reactivation(host):
    tasks, execution, plugins, service, _ = host
    task = await tasks.create(configuration())
    await service.disable(PLUGIN)
    await execution.tick()
    paused = await tasks.get(task.task_id)
    assert not paused.enabled and paused.status == "paused"
    assert "unavailable" in paused.error
    assert not await tasks.executions(task.task_id)
    assert PLUGIN not in plugins._active
    with pytest.raises(ValueError, match="unavailable"):
        await tasks.set_enabled(task.task_id, True, expected_revision=paused.revision)


async def test_disable_settles_inflight_service_and_releases_implementation(host):
    tasks, execution, plugins, service, _ = host
    task = await tasks.create(configuration(block=True))
    generation = plugins._active[PLUGIN]
    await execution.tick()
    await started(generation.services)
    await service.disable(PLUGIN)
    receipt = await settle(tasks, execution, task.task_id)
    assert receipt.status == "interrupted", receipt.error
    assert generation.lease_count == 0
    assert not tasks.runtime.sessions.catalog().entries


async def test_unobserved_claim_is_interrupted_without_replaying_service(host):
    tasks, execution, plugins, _, _ = host
    task = await tasks.create(configuration())
    binding = await tasks.bind_plugin_task(task.configuration.target)
    try:
        await tasks.claim(task, binding.target)
    finally:
        binding.close()
    await execution.recover()
    receipt = (await tasks.executions(task.task_id))[0]
    assert receipt.status == "interrupted" and "not replayed" in receipt.error
    assert await plugins._active[PLUGIN].services.call(PLUGIN, "sync", "inspect") == 0


async def test_peer_interrupt_request_is_consumed_by_the_scheduling_owner(host):
    tasks, execution, plugins, _, _ = host
    task = await tasks.create(configuration(block=True))
    await execution.tick()
    await started(plugins._active[PLUGIN].services)
    peer = TaskService(type(tasks.repository)(plugins.store.home), tasks.runtime)
    await peer.request_interrupt(await peer.get(task.task_id))
    receipt = await settle(tasks, execution, task.task_id)
    assert receipt.status == "interrupted" and receipt.cancel_requested
    assert plugins._active[PLUGIN].lease_count == 0


async def test_running_upgrade_finishes_old_contract_and_next_occurrence_uses_new(
    host, tmp_path, kind
):
    tasks, execution, plugins, service, clock = host
    task = await tasks.create(configuration(block=True))
    old = plugins._active[PLUGIN]
    await execution.tick()
    await started(old.services)
    accepted = (await tasks.executions(task.task_id))[0].target
    await service.reload(PLUGIN, package(tmp_path, kind, version="1.1.0", label="two"))
    assert plugins._active[PLUGIN] is not old
    assert not old.stopped
    await old.services.call(PLUGIN, "sync", "release")
    first = await settle(tasks, execution, task.task_id)
    assert first.status == "completed", first.error
    assert first.target == accepted
    assert first.result["label"] == "one"
    assert old.lease_count == 0
    clock[0] += timedelta(hours=1)
    # The future plan binds the currently selected service when it is claimed.
    current = await tasks.get(task.task_id)
    await tasks.update(task.task_id, configuration(), expected_revision=current.revision)
    await execution.tick()
    second = await settle(tasks, execution, task.task_id)
    assert second.result["label"] == "two"
    assert second.target.implementation.release_id != first.target.implementation.release_id


@pytest.mark.parametrize("restriction", ["schedulable", "permissions"])
async def test_service_requires_explicit_schedule_declaration_and_permission(
    host, tmp_path, kind, restriction
):
    tasks, _, _, service, _ = host
    await service.reload(PLUGIN, package(tmp_path, kind, version="1.1.0", **{restriction: False}))
    with pytest.raises((ValueError, PermissionError), match="schedulable|background.run"):
        await tasks.create(configuration())
    assert not await tasks.list()


async def test_handler_failure_records_result_without_retrying_occurrence(host):
    tasks, execution, plugins, _, _ = host
    task = await tasks.create(configuration(fail=True))
    await execution.tick()
    result = await settle(tasks, execution, task.task_id)
    assert result.status == "failed" and "Sync failed" in result.error
    await execution.tick()
    assert len(await tasks.executions(task.task_id)) == 1
    assert await plugins._active[PLUGIN].services.call(PLUGIN, "sync", "inspect") == 1


async def test_handler_arguments_do_not_alias_the_persisted_execution_target(host):
    tasks, execution, _, _, _ = host
    task = await tasks.create(configuration(mutate=True))
    await execution.tick()
    receipt = await settle(tasks, execution, task.task_id)
    assert receipt.status == "completed", receipt.error
    assert receipt.target.request.arguments == {"mutate": True}
    assert receipt.result["arguments"] == {"mutate": True, "handler_value": "local"}


async def test_queued_cancellation_before_handler_entry_releases_lease(host):
    tasks, execution, plugins, _, _ = host
    task = await tasks.create(configuration())
    await execution.tick()
    job = next(iter(execution._plugin_jobs.values()))
    job.cancel()
    receipt = await settle(tasks, execution, task.task_id)
    assert receipt.status == "interrupted"
    assert plugins._active[PLUGIN].lease_count == 0


async def test_close_cancels_plugin_execution_before_plugin_runtime_shutdown(host):
    tasks, execution, plugins, _, _ = host
    task = await tasks.create(configuration(block=True))
    await execution.tick()
    await started(plugins._active[PLUGIN].services)
    await execution.close()
    receipt = (await tasks.executions(task.task_id))[0]
    assert receipt.status == "interrupted"
    assert plugins._active[PLUGIN].lease_count == 0


async def test_scheduler_settles_plugin_jobs_before_stopping_owned_services(host):
    tasks, execution, plugins, _, _ = host
    task = await tasks.create(configuration(block=True))
    generation = plugins._active[PLUGIN]
    observed = []

    class Services:
        async def start_home_services(self):
            pass

        async def stop_home_services(self):
            observed.append(generation.gate.in_flight)

    scheduler = TaskScheduler(tasks, execution, owned_services=Services(), refresh_seconds=0.01)
    await scheduler.start()
    try:
        await started(generation.services)
        await scheduler.stop()
        assert observed == [0]
        assert (await tasks.executions(task.task_id))[0].status == "interrupted"
    finally:
        await scheduler.close()


async def test_plugin_and_agent_targets_share_the_dispatch_concurrency_limit(host):
    tasks, execution, plugins, _, _ = host
    plugin_task = await tasks.create(configuration(block=True))
    await execution.tick()
    await started(plugins._active[PLUGIN].services)
    agent_task = await tasks.create(
        TaskConfiguration(
            target=AgentTaskTarget(
                prompt="Reply", workspace=str(plugins.store.home), model="faux/task"
            ),
            schedule=TaskSchedule(frequency="hourly", starts_at=NOW),
        )
    )
    await execution.tick()
    assert not await tasks.executions(agent_task.task_id)
    assert not tasks.runtime.sessions.catalog().entries
    await plugins._active[PLUGIN].services.call(PLUGIN, "sync", "release")
    assert (await settle(tasks, execution, plugin_task.task_id)).status == "completed"
    await execution.tick()
    assert (await settle(tasks, execution, agent_task.task_id)).status == "completed"
    assert (await tasks.counts()).completed_executions == 2


@pytest.mark.parametrize("kind", ["worker"])
async def test_worker_exit_settles_claim_without_replay_or_retained_lease(host):
    tasks, execution, plugins, _, _ = host
    task = await tasks.create(configuration(block=True))
    generation = plugins._active[PLUGIN]
    await execution.tick()
    await started(generation.services)
    generation.instance.client.process.kill()
    receipt = await settle(tasks, execution, task.task_id)
    # Transport failure or generation revocation may settle first; neither is
    # evidence that the external effect did not happen.
    assert receipt.status in {"failed", "interrupted"} and receipt.error
    assert generation.lease_count == 0
    await execution.tick()
    assert len(await tasks.executions(task.task_id)) == 1
    assert not execution._plugin_jobs
