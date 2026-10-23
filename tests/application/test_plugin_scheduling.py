from __future__ import annotations

import asyncio
import json
import shutil
import sys
from contextvars import copy_context
from datetime import timedelta
from pathlib import Path

import pytest

from ouro.core.tasks import AgentTaskRun, AgentTaskTarget, TaskConfiguration, TaskSchedule
from ouro.local_application import LocalApplication
from ouro.plugins.invocation import plugin_invocation
from ouro.plugins.worker.codec import WorkerProtocolError
from tests.application.test_plugin_tasks import (
    NOW,
    PLUGIN,
    configuration,
    package,
    settle,
    started,
)
from tests.application.test_plugin_tasks import host as host
from tests.application.test_plugin_tasks import kind as kind
from tests.application.test_tasks import SelectivelyBlockingProvider


def native_scheduler(plugins, kind):
    instance = plugins._active[PLUGIN].instance
    return instance.scheduler if kind == "python-inprocess" else instance.scheduler_bridge.scheduler


async def test_scheduler_rejects_late_callback_and_revoked_generation_handles(host, kind):
    tasks, _, plugins, service, _ = host
    scheduler = native_scheduler(plugins, kind)
    with plugin_invocation(PLUGIN, writable=True):
        callback_context = copy_context()
    with pytest.raises(RuntimeError, match="invocation has finished"):
        await asyncio.create_task(scheduler.list(), context=callback_context)
    await service.disable(PLUGIN)
    with pytest.raises(RuntimeError, match="generation is unavailable"):
        await scheduler.list()
    assert not await tasks.list()


async def schedule(plugins, operation, *, read_only=False, **arguments):
    return await plugins._active[PLUGIN].services.call(
        PLUGIN,
        "sync",
        "read_schedule" if read_only else "schedule",
        {"operation": operation, **arguments},
    )


async def test_sdk_manages_only_owned_plans_and_exposes_service_receipts(host):
    tasks, execution, plugins, _, _ = host
    unowned = await tasks.create(configuration())
    await tasks.set_enabled(unowned.task_id, False, expected_revision=unowned.revision)
    created = await schedule(
        plugins, "create", configuration=configuration(value=1).model_dump(mode="json")
    )
    task_id = created["task_id"]
    owned = await tasks.get(task_id)
    assert owned.owner_id == PLUGIN
    assert await schedule(plugins, "get", task_id=task_id, read_only=True) == created
    assert await schedule(plugins, "list", read_only=True) == [created]
    assert await schedule(plugins, "list", offset=1, limit=1) == []

    updated = await schedule(
        plugins,
        "update",
        task_id=task_id,
        expected_revision=created["revision"],
        configuration=configuration(value=2).model_dump(mode="json"),
    )
    paused = await schedule(
        plugins,
        "set_enabled",
        task_id=task_id,
        enabled=False,
        expected_revision=updated["revision"],
    )
    await execution.tick()
    assert not await tasks.executions(task_id)
    await schedule(
        plugins, "set_enabled", task_id=task_id, enabled=True, expected_revision=paused["revision"]
    )
    await execution.tick()
    receipt = await settle(tasks, execution, task_id)
    assert receipt.owner_id == PLUGIN and receipt.status == "completed"
    history = await schedule(plugins, "executions", task_id=task_id, read_only=True)
    assert len(history) == 1
    assert history[0]["execution_id"] == receipt.execution_id
    assert history[0]["result"]["arguments"] == {"value": 2}
    assert history[0]["session_id"] is None and history[0]["run_id"] is None
    latest = await schedule(plugins, "get", task_id=task_id)
    await schedule(plugins, "delete", task_id=task_id, expected_revision=latest["revision"])
    assert await schedule(plugins, "list") == []
    assert (await tasks.get(unowned.task_id)).owner_id is None


async def test_sdk_rejects_foreign_owner_reads_and_mutations_before_effects(host):
    tasks, _, plugins, _, _ = host
    foreign = await tasks.create(configuration())
    operations = {
        "get": {},
        "executions": {},
        "update": {
            "expected_revision": foreign.revision,
            "configuration": foreign.configuration.model_dump(mode="json"),
        },
        "set_enabled": {"expected_revision": foreign.revision, "enabled": False},
        "delete": {"expected_revision": foreign.revision},
        "interrupt": {"expected_revision": foreign.revision},
    }
    for operation, arguments in operations.items():
        with pytest.raises((PermissionError, WorkerProtocolError), match="another owner"):
            await schedule(plugins, operation, task_id=foreign.task_id, **arguments)
    assert await tasks.get(foreign.task_id) == foreign
    assert not await tasks.executions(foreign.task_id)


async def test_sdk_read_only_callbacks_can_query_but_cannot_change_plans(host):
    tasks, _, plugins, _, _ = host
    config = configuration().model_dump(mode="json")
    created = await schedule(plugins, "create", configuration=config)
    task_id, revision = created["task_id"], created["revision"]
    before = await tasks.get(task_id)
    operations = {
        "create": {"configuration": config},
        "update": {"task_id": task_id, "expected_revision": revision, "configuration": config},
        "set_enabled": {"task_id": task_id, "expected_revision": revision, "enabled": False},
        "delete": {"task_id": task_id, "expected_revision": revision},
        "interrupt": {"task_id": task_id, "expected_revision": revision},
    }
    for operation, arguments in operations.items():
        with pytest.raises((PermissionError, WorkerProtocolError), match="read-only"):
            await schedule(plugins, operation, read_only=True, **arguments)
    assert await schedule(plugins, "list", read_only=True) == [created]
    assert await tasks.list() == (before,)


async def test_sdk_stale_revision_cannot_overwrite_or_control_newer_plan(host):
    tasks, _, plugins, _, _ = host
    created = await schedule(
        plugins, "create", configuration=configuration(value="before").model_dump(mode="json")
    )
    task_id, revision = created["task_id"], created["revision"]
    updated = await tasks.update(task_id, configuration(value="after"), expected_revision=revision)
    operations = {
        "update": {"configuration": created["configuration"]},
        "set_enabled": {"enabled": False},
        "delete": {},
        "interrupt": {},
    }
    for operation, arguments in operations.items():
        with pytest.raises((ValueError, WorkerProtocolError), match="Task changed"):
            await schedule(
                plugins, operation, task_id=task_id, expected_revision=revision, **arguments
            )
    assert await tasks.get(task_id) == updated


async def test_sdk_interrupt_is_a_durable_request_to_the_scheduling_owner(host):
    tasks, execution, plugins, _, _ = host
    created = await schedule(
        plugins, "create", configuration=configuration(block=True).model_dump(mode="json")
    )
    task_id = created["task_id"]
    await execution.tick()
    await started(plugins._active[PLUGIN].services)
    active = await schedule(plugins, "get", task_id=task_id)
    await schedule(plugins, "interrupt", task_id=task_id, expected_revision=active["revision"])
    assert (await tasks.executions(task_id))[0].cancel_requested
    receipt = await settle(tasks, execution, task_id)
    assert receipt.status == "interrupted" and receipt.owner_id == PLUGIN
    await execution.tick()
    assert len(await tasks.executions(task_id)) == 1


def agent_configuration(workspace):
    return TaskConfiguration(
        target=AgentTaskTarget(
            prompt="scheduled reply", workspace=str(workspace), model="faux/task"
        ),
        schedule=TaskSchedule(frequency="hourly", starts_at=NOW),
    ).model_dump(mode="json")


async def test_sdk_agent_plan_requires_sessions_permission_and_records_native_run(
    host, tmp_path, kind
):
    tasks, execution, plugins, service, _ = host
    config = agent_configuration(tmp_path)
    with pytest.raises((PermissionError, WorkerProtocolError), match="sessions.write"):
        await schedule(plugins, "create", configuration=config)
    assert not await tasks.list()
    await service.reload(PLUGIN, package(tmp_path, kind, version="1.1.0", sessions=True))
    created = await schedule(plugins, "create", configuration=config)
    await execution.tick()
    receipt = await settle(tasks, execution, created["task_id"])
    assert receipt.status == "completed" and receipt.owner_id == PLUGIN
    assert isinstance(receipt.target, AgentTaskRun)
    run = tasks.runtime.store.load_run(receipt.target.session_id, receipt.target.run_id)
    assert run.status == "completed" and run.trigger == "scheduled"
    assert run.initiator_id == f"ouro.tasks/{created['task_id']}"
    history = await schedule(plugins, "executions", task_id=created["task_id"])
    assert history[0]["session_id"] == run.session_id and history[0]["run_id"] == run.run_id


async def test_sdk_permission_removal_blocks_new_writes_and_future_dispatch(host, tmp_path, kind):
    tasks, execution, plugins, service, _ = host
    created = await schedule(
        plugins, "create", configuration=configuration().model_dump(mode="json")
    )
    await service.reload(PLUGIN, package(tmp_path, kind, version="1.1.0", permissions=False))
    assert await schedule(plugins, "list", read_only=True) == [created]
    with pytest.raises((PermissionError, WorkerProtocolError), match="background.run"):
        await schedule(
            plugins, "delete", task_id=created["task_id"], expected_revision=created["revision"]
        )
    await execution.tick()
    paused = await tasks.get(created["task_id"])
    assert not paused.enabled and "background.run" in paused.error
    assert not await tasks.executions(paused.task_id)


async def test_sdk_can_schedule_only_own_or_declared_dependency_services(host, tmp_path, kind):
    tasks, execution, plugins, service, _ = host
    dependency = "dev.example.sync_provider"
    provider_source = package(tmp_path / "provider", kind)
    manifest_path = provider_source / ".ouro-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(plugin_id=dependency, namespace="sync_provider")
    if kind == "python-inprocess":
        (provider_source / "scheduled_plugin.py").rename(
            provider_source / "sync_provider_plugin.py"
        )
        manifest["runtime"]["entrypoint"] = "sync_provider_plugin:create_plugin"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    service.install(provider_source)
    assert (await service.enable(dependency)).status == "active"
    config = configuration(block=True).model_dump(mode="json")
    config["target"]["plugin_id"] = dependency
    with pytest.raises((PermissionError, WorkerProtocolError), match="declared dependency"):
        await schedule(plugins, "create", configuration=config)
    assert not await tasks.list()

    owner_source = package(tmp_path, kind, version="1.1.0")
    manifest_path = owner_source / ".ouro-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dependencies"] = {"plugins": [{"plugin_id": dependency, "version": "^1.0.0"}]}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    await service.reload(PLUGIN, owner_source)
    created = await schedule(plugins, "create", configuration=config)
    await execution.tick()
    provider = plugins._active[dependency]
    async with asyncio.timeout(5):
        while not await provider.services.call(dependency, "sync", "inspect"):
            await asyncio.sleep(0.01)
    await service.disable(PLUGIN)
    receipt = await settle(tasks, execution, created["task_id"])
    assert receipt.owner_id == PLUGIN and receipt.target.request.plugin_id == dependency
    assert receipt.status == "interrupted"
    assert plugins._active[dependency] is provider and provider.gate.is_open
    assert provider.lease_count == 0


async def test_disabling_owner_wakes_scheduler_and_interrupts_its_agent_run(host, tmp_path, kind):
    tasks, execution, plugins, service, clock = host
    provider = SelectivelyBlockingProvider()
    tasks.runtime.adapter.provider = provider
    await service.reload(PLUGIN, package(tmp_path, kind, version="1.1.0", sessions=True))
    created = await schedule(plugins, "create", configuration=agent_configuration(tmp_path))
    task_id = created["task_id"]
    await execution.tick()
    async with asyncio.timeout(5):
        await provider.started.wait()
    tasks.changed.clear()
    await service.disable(PLUGIN)
    assert tasks.changed.is_set()
    receipt = await settle(tasks, execution, task_id)
    assert receipt.status == "interrupted" and receipt.owner_id == PLUGIN
    run = tasks.runtime.store.load_run(receipt.target.session_id, receipt.target.run_id)
    assert run.metadata["abort_reason"] == "Task owner is unavailable"
    clock[0] += timedelta(hours=1)
    await execution.tick()
    paused = await tasks.get(task_id)
    assert not paused.enabled and "owner is unavailable" in paused.error
    assert len(await tasks.executions(task_id)) == 1


async def test_unpublished_candidate_cannot_create_a_plan(host, tmp_path, kind):
    tasks, _, plugins, service, _ = host
    original = plugins._active[PLUGIN]
    source = package(tmp_path, kind, version="1.1.0")
    code = source / "scheduled_plugin.py"
    raw = json.dumps(configuration().model_dump(mode="json"))
    code.write_text(
        code.read_text(encoding="utf-8").replace(
            "async def start(self):\n        pass",
            "async def start(self):\n        import json\n"
            f"        await self.scheduler.create(decode_spec(json.loads({raw!r})))",
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="generation is unavailable"):
        await service.reload(PLUGIN, source)
    assert plugins._active[PLUGIN] is original
    assert not await tasks.list()


def background_package(tmp_path):
    source = package(tmp_path, "python-inprocess", version="1.1.0")
    code = source / "scheduled_plugin.py"
    raw = json.dumps(configuration().model_dump(mode="json"))
    code.write_text(
        code.read_text(encoding="utf-8")
        .replace(
            'services=[ServiceContribution("sync", self.methods)]',
            'services=[ServiceContribution("sync", self.methods)], '
            "background_services=[sdk.BackgroundServiceContribution("
            '"setup", self.setup, lambda: None)]',
        )
        .replace(
            "async def start(self):",
            "async def setup(self):\n        import json\n"
            f"        await self.scheduler.create(decode_spec(json.loads({raw!r})))\n\n"
            "    async def start(self):",
        ),
        encoding="utf-8",
    )
    manifest_path = source / ".ouro-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["contributes"] = {"background_services": [{"id": "setup"}]}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return source


@pytest.mark.parametrize("kind", ["python-inprocess"])
async def test_background_start_can_create_plan_after_publication_without_deadlock(
    host, tmp_path, kind
):
    tasks, _, _, service, _ = host
    source = background_package(tmp_path)
    async with asyncio.timeout(10):
        result = await service.reload(PLUGIN, source)
    assert result.status == "active", service.diagnostics()
    plans = await tasks.list(owner_id=PLUGIN)
    assert len(plans) == 1 and plans[0].configuration == configuration()


@pytest.mark.parametrize("task_backend", ["files", "sqlite"])
async def test_public_scheduler_is_ready_before_the_dispatch_loop_starts(tmp_path, task_backend):
    application = LocalApplication.build(tmp_path / "home", task_backend=task_backend)
    service = application.plugins
    service.install(background_package(tmp_path))
    try:
        result = await service.enable(PLUGIN)
        assert result.status == "active", service.diagnostics()
        plans = await application.tasks.list(owner_id=PLUGIN)
        assert len(plans) == 1
        if task_backend == "files":
            assert (tmp_path / "home/tasks/layout.json").is_file()
        await application.start()
        assert not application.task_scheduler.diagnostics
        assert plans[0].task_id in {
            plan.task_id for plan in await application.tasks.list(owner_id=PLUGIN)
        }
    finally:
        await application.close()


async def test_public_sync_example_executes_preserves_data_and_manages_plans(host, tmp_path, kind):
    tasks, execution, plugins, service, clock = host
    source = tmp_path / "sync-example"
    shutil.copytree(Path(__file__).parents[2] / "examples/plugins/scheduled-sync", source)
    manifest_path = source / ".ouro-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    example = manifest["plugin_id"]
    if kind == "python-inprocess":
        manifest["runtime"] = {"kind": kind, "entrypoint": "scheduled_sync_plugin:create_plugin"}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    service.install(source)
    assert (await service.enable(example)).status == "active", service.diagnostics()
    assert not await tasks.list(owner_id=example)
    data = plugins.store.data_root / example
    data.mkdir(parents=True, exist_ok=True)
    inbox = {"schema_version": 1, "records": [{"id": "ouro", "text": "Local agent"}]}
    (data / "inbox.json").write_text(json.dumps(inbox), encoding="utf-8")
    output = await service.execute_command(f"{example}:sync-plan", "create")
    plans = await tasks.list(owner_id=example)
    assert len(plans) == 1 and plans[0].task_id in output.output
    task_id = plans[0].task_id
    clock[0] = plans[0].next_run_at
    await execution.tick()
    receipt = await settle(tasks, execution, task_id)
    assert receipt.status == "completed", receipt.error
    assert receipt.result == {"records": 1}
    snapshot = json.loads((data / "snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["records"] == {"ouro": "Local agent"}
    assert snapshot["execution_id"] == receipt.execution_id

    manifest["version"] = "0.2.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert (await service.reload(example, source)).status == "active"
    assert len(await tasks.list(owner_id=example)) == 1
    assert json.loads((data / "snapshot.json").read_text(encoding="utf-8")) == snapshot
    await service.execute_command(f"{example}:sync-plan", f"pause {task_id}")
    assert not (await tasks.get(task_id)).enabled
    clock[0] += timedelta(hours=1)
    (data / "inbox.json").write_text('{"schema_version": 99}', encoding="utf-8")
    await service.execute_command(f"{example}:sync-plan", f"resume {task_id}")
    await execution.tick()
    failed = await settle(tasks, execution, task_id)
    assert failed.status == "failed" and "schema_version 1" in failed.error
    assert failed.target.implementation.release_id != receipt.target.implementation.release_id
    assert json.loads((data / "snapshot.json").read_text(encoding="utf-8")) == snapshot
    listing = await service.execute_command(f"{example}:sync-plan", "list")
    assert task_id in listing.output
    await service.execute_command(f"{example}:sync-plan", f"delete {task_id}")
    await service.disable(example)
    assert not await tasks.list(owner_id=example)
    assert (data / "snapshot.json").exists()

    maintenance = [sys.executable, str(source / "maintain.py")]
    backup = tmp_path / "snapshot-backup.json"
    saved = await service.maintain_data(example, [*maintenance, "backup", str(backup)])
    assert saved.status == "succeeded", saved.stderr
    assert json.loads(backup.read_bytes()) == snapshot
    unsupported = b'{"schema_version": 99, "records": {"keep": "this data"}}'
    (data / "snapshot.json").write_bytes(unsupported)
    assert (await service.enable(example)).status == "failed"
    assert "snapshot schema_version 1" in str(service.diagnostics(example))
    assert not plugins.has_resources(example)
    assert (data / "snapshot.json").read_bytes() == unsupported
    with pytest.raises(ValueError, match="must be disabled"):
        await service.maintain_data(example, [*maintenance, "backup", str(backup)])
    await service.disable(example)
    assert not plugins.store.read_state().plugins[example].activation.desired_enabled

    damaged = tmp_path / "snapshot-unsupported.json"
    failed = await service.maintain_data(example, [*maintenance, "backup", str(backup)])
    assert failed.status == "failed"
    assert json.loads(backup.read_bytes()) == snapshot
    assert (data / "snapshot.json").read_bytes() == unsupported
    restored = await service.maintain_data(
        example, [*maintenance, "restore", str(backup), "--save-current", str(damaged)]
    )
    assert restored.status == "succeeded", restored.stderr
    assert damaged.read_bytes() == unsupported
    assert json.loads((data / "snapshot.json").read_bytes()) == snapshot
    assert not plugins.store.read_state().plugins[example].activation.desired_enabled
    rejected = await service.maintain_data(
        example, [*maintenance, "restore", str(damaged), "--save-current", str(tmp_path / "unused")]
    )
    assert rejected.status == "failed" and "snapshot schema_version 1" in rejected.stderr
    assert not (tmp_path / "unused").exists()
    assert json.loads((data / "snapshot.json").read_bytes()) == snapshot
    assert (await service.enable(example)).status == "active"
    await service.disable(example)
