from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ouro.core.session_controls import HostSessionInput
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from tests.application.test_run_drivers import DRIVER, PLUGIN, host, plugin_source, set_driver_body
from tests.application.test_runtime import GateProvider

DEPENDENCY = "dev.ouro.driver_dependency"
FIXTURES = Path(__file__).parents[1] / "plugins/fixtures/driver_contracts"


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def configured_packages(tmp_path, kind="python-inprocess", *, dependency_kind="python-inprocess"):
    driver = plugin_source(tmp_path, kind)
    manifest_path = driver / ".ouro-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["contributes"].pop("commands")
    manifest["configuration"] = {"schema": "./config.json", "version": 1}
    manifest["dependencies"] = {"plugins": [{"plugin_id": DEPENDENCY, "version": "^1.0.0"}]}
    write_json(manifest_path, manifest)
    write_json(driver / "config.json", {"type": "object"})
    (driver / "review_driver_plugin.py").write_text(
        (FIXTURES / "review_driver_plugin.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    if kind == "worker":
        (driver / "worker.py").write_text(
            (FIXTURES / "driver_worker.py").read_text(encoding="utf-8"), encoding="utf-8"
        )

    dependency = tmp_path / "dependency"
    (dependency / ".ouro-plugin").mkdir(parents=True)
    write_json(
        dependency / ".ouro-plugin/plugin.json",
        {
            "schema_version": 3,
            "plugin_id": DEPENDENCY,
            "namespace": "driver_dependency",
            "name": "Driver dependency",
            "version": "1.0.0",
            "engines": {"ouro": ">=0.4.0", "plugin_api": "^5.0.0"},
            "runtime": {
                "kind": "python-inprocess",
                "entrypoint": "driver_dependency_plugin:create",
            },
            "configuration": {"schema": "./config.json", "version": 1},
            "exports": [{"id": "value", "methods": {"read": {"effect": "read_only"}}}],
            "contributes": {"background_services": [{"id": "watcher"}]},
        },
    )
    write_json(dependency / "config.json", {"type": "object"})
    (dependency / "driver_dependency_plugin.py").write_text(
        (FIXTURES / "driver_dependency_plugin.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    if dependency_kind == "worker":
        manifest_path = dependency / ".ouro-plugin/plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["runtime"] = {
            "kind": "worker",
            "command": ["{python}", "worker.py"],
            "protocol": "3.0",
        }
        write_json(manifest_path, manifest)
        (dependency / "worker.py").write_text(
            (FIXTURES / "service_worker.py").read_text(encoding="utf-8"), encoding="utf-8"
        )
    return driver, dependency


async def install_original(service, packages, *, wait=False):
    driver, dependency = packages
    service.install(dependency)
    service.install(driver)
    await service.configure(
        service.configuration(DEPENDENCY).candidate(values={"label": "old dependency"})
    )
    await service.configure(
        service.configuration(PLUGIN).candidate(values={"label": "old driver", "wait": wait})
    )
    assert (await service.enable(PLUGIN)).status == "active"


async def upgrade(service, packages):
    driver, dependency = packages
    # The new Driver no longer depends on the service. Its old accepted work does.
    manifest_path = driver / ".ouro-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "0.1.1"
    manifest["dependencies"] = {"plugins": []}
    write_json(manifest_path, manifest)
    installed = service.install(driver)
    await service.configure(
        service.configuration(PLUGIN, package_digest=installed.package_digest).candidate(
            values={"label": "new driver"}
        )
    )
    assert (await service.select(PLUGIN, installed.package_digest)).status == "active"
    manifest_path = dependency / ".ouro-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "1.1.0"
    write_json(manifest_path, manifest)
    (dependency / "driver_dependency_plugin.py").write_text(
        (FIXTURES / "driver_dependency_plugin.py")
        .read_text(encoding="utf-8")
        .replace("original:", "replacement:"),
        encoding="utf-8",
    )
    installed = service.install(dependency)
    await service.configure(
        service.configuration(DEPENDENCY, package_digest=installed.package_digest).candidate(
            values={"label": "new dependency"}
        )
    )
    assert (await service.select(DEPENDENCY, installed.package_digest)).status == "active"


async def approve(runtime, pending):
    return await runtime.approve_tool(
        session_id=pending.session.session_id,
        run_id=pending.run.run_id,
        approval_id=pending.run.pending_approval_id,
        approved=True,
    )


async def test_queued_input_retains_driver_dependencies_until_native_completion(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    packages = configured_packages(tmp_path, "worker")
    try:
        await install_original(service, packages)
        session = runtime.create_session()
        receipt = await runtime.submit_host_input(
            HostSessionInput(prompt="deploy", host_source_id="test", driver_id=DRIVER),
            session_id=session.session_id,
        )
        selected = runtime.queued_inputs.bound_drivers(
            store.load_session_record(session.session_id)
        )[0]
        await upgrade(service, packages)
        for implementation in selected.implementations:
            await plugins.wait_stopped(implementation.plugin_id)
            removed = await service.remove(
                implementation.plugin_id, package_digest=implementation.release_id
            )
            assert removed.retained_by == [f"{session.session_id}/queued"]
            service.install(
                plugins.store.package_path(implementation.plugin_id, implementation.release_id)
            )
        await runtime.close()
        await plugins.close()
        runtime, plugins, service = host(tmp_path, store, [])
        await plugins.start()
        pending = await runtime.consume_host_input(receipt)
        assert pending.run.status == "waiting_approval", pending.run.error
        assert pending.run.driver == selected
        result = await approve(runtime, pending)
        assert result.run.status == "completed", result.run.error
        assert result.final_text == "old driver/original:old dependency"
        for implementation in selected.implementations:
            assert (
                await service.remove(
                    implementation.plugin_id, package_digest=implementation.release_id
                )
            ).changed
    finally:
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize(
    ("store_type", "kind"),
    [(LocalSessionStore, "python-inprocess"), (SQLiteSessionStore, "worker")],
)
async def test_restart_loads_original_driver_configuration_and_dependency_without_reselection(
    tmp_path, store_type, kind
):
    store = store_type(
        tmp_path / ("sessions.sqlite3" if store_type is SQLiteSessionStore else "sessions")
    )
    store.initialize()
    calls = []
    runtime, plugins, service = host(tmp_path, store, calls)
    packages = configured_packages(tmp_path, kind)
    try:
        await install_original(service, packages)
        pending = await runtime.run("deploy", driver_id=DRIVER)
        assert pending.run.status == "waiting_approval", pending.run.error
        await upgrade(service, packages)
        old_dependency = pending.run.driver.dependencies[0]
        await plugins.wait_stopped(DEPENDENCY)
        removed = await service.remove(DEPENDENCY, package_digest=old_dependency.release_id)
        assert removed.retained_by == [f"{pending.session.session_id}/{pending.run.run_id}"]
        service.install(plugins.store.package_path(DEPENDENCY, old_dependency.release_id))
        await runtime.close()
        await plugins.close()
        runtime, plugins, service = host(tmp_path, store, calls)
        await plugins.start()
        current = plugins.store.read_state()
        result = await approve(runtime, pending)
        assert result.run.status == "completed", result.run.error
        assert result.final_text == "old driver/original:old dependency"
        assert plugins.store.read_state() == current
        assert not plugins._retired
        assert len(calls) == 1
        assert (await service.remove(DEPENDENCY, package_digest=old_dependency.release_id)).changed
    finally:
        await runtime.close()
        await plugins.close()


async def test_same_package_configuration_change_preserves_the_waiting_driver_configuration(
    tmp_path,
):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    try:
        await install_original(service, configured_packages(tmp_path))
        pending = await runtime.run("deploy", driver_id=DRIVER)
        await service.configure(
            service.configuration(PLUGIN).candidate(values={"label": "new driver"})
        )
        await service.configure(
            service.configuration(DEPENDENCY).candidate(values={"label": "new dependency"})
        )
        result = await approve(runtime, pending)
        assert result.run.status == "completed", result.run.error
        assert result.final_text == "old driver/original:old dependency"
        assert not plugins._retired
    finally:
        await runtime.close()
        await plugins.close()


async def test_removing_an_unused_release_keeps_configuration_referenced_by_another_release(
    tmp_path,
):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    packages = configured_packages(tmp_path)
    try:
        await install_original(service, packages)
        pending = await runtime.run("deploy", driver_id=DRIVER)
        await service.configure(
            service.configuration(PLUGIN).candidate(values={"label": "new configuration"})
        )
        driver, _ = packages
        manifest_path = driver / ".ouro-plugin/plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "0.1.1"
        write_json(manifest_path, manifest)
        unused = service.install(driver)
        await service.configure(
            service.configuration(PLUGIN, package_digest=unused.package_digest).candidate(
                values={"label": "old driver", "wait": False}
            )
        )
        await service.remove(PLUGIN, package_digest=unused.package_digest)
        result = await approve(runtime, pending)
        assert result.final_text == "old driver/original:old dependency", result.run.error
    finally:
        await runtime.close()
        await plugins.close()


async def test_disabled_dependency_preserves_checkpoint_and_can_resume_after_enable(tmp_path):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    calls = []
    runtime, plugins, service = host(tmp_path, store, calls)
    packages = configured_packages(tmp_path)
    try:
        await install_original(service, packages)
        pending = await runtime.run("deploy", driver_id=DRIVER)
        await upgrade(service, packages)
        assert (await service.disable(DEPENDENCY)).status == "disabled"
        result = await approve(runtime, pending)
        assert result.run.status == "failed"
        assert f"implementation is disabled: {DEPENDENCY}" in result.run.error
        assert result.run.driver_checkpoint.state == "ready"
        assert not plugins.has_resources(DEPENDENCY)
        assert (await service.enable(DEPENDENCY)).status == "active"
        result = await runtime.continue_run(session_id=pending.session.session_id)
        assert result.final_text == "old driver/original:old dependency", result.run.error
        assert len(calls) == 1
    finally:
        await runtime.close()
        await plugins.close()


async def test_disabling_dependency_revokes_old_driver_using_its_original_dependency_graph(
    tmp_path,
):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    packages = configured_packages(tmp_path)
    running = None
    try:
        await install_original(service, packages, wait=True)
        pending = await runtime.run("deploy", driver_id=DRIVER)
        await upgrade(service, packages)
        running = asyncio.create_task(approve(runtime, pending))
        async with asyncio.timeout(5):
            while not any(
                item.record.plugin_id == PLUGIN and item.instance.entered.is_set()
                for item in plugins._retired.values()
            ):
                await asyncio.sleep(0)
        assert (await service.disable(DEPENDENCY)).status == "disabled"
        async with asyncio.timeout(5):
            result = await running
        assert result.run.status == "failed"
        assert "unavailable" in result.run.error
        assert not plugins.has_resources(DEPENDENCY)
        assert plugins.status(PLUGIN) == "active"
        assert not plugins._retired
    finally:
        if running is not None and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await runtime.close()
        await plugins.close()


async def test_concurrent_bindings_share_loaded_contract_until_the_last_invocation_releases_it(
    tmp_path,
):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    packages = configured_packages(tmp_path)
    try:
        await install_original(service, packages)
        pending = await runtime.run("deploy", driver_id=DRIVER)
        await upgrade(service, packages)
        async with plugins.bind_driver(pending.run.driver) as first:
            retained = tuple(plugins._retired)
            assert len(retained) == 2
            assert plugins._active[DEPENDENCY].instance.background_started
            assert all(not item.started_services for item in plugins._retired.values())
            async with plugins.bind_driver(pending.run.driver) as second:
                assert first.value.owner == second.value.owner
                assert tuple(plugins._retired) == retained
            assert tuple(plugins._retired) == retained
        assert not plugins._retired
    finally:
        await runtime.close()
        await plugins.close()


async def test_retained_dependency_worker_exit_revokes_its_driver_without_stopping_current_versions(
    tmp_path,
):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    packages = configured_packages(tmp_path, dependency_kind="worker")
    running = None
    try:
        await install_original(service, packages, wait=True)
        pending = await runtime.run("deploy", driver_id=DRIVER)
        await upgrade(service, packages)
        running = asyncio.create_task(approve(runtime, pending))
        async with asyncio.timeout(5):
            while not any(
                item.record.plugin_id == PLUGIN and item.instance.entered.is_set()
                for item in plugins._retired.values()
            ):
                await asyncio.sleep(0)
        old = next(
            item for item in plugins._retired.values() if item.record.plugin_id == DEPENDENCY
        )
        old.instance.client.process.kill()
        async with asyncio.timeout(5):
            result = await running
        assert result.run.status == "failed"
        assert "unavailable" in result.run.error
        assert not plugins._retired
        assert plugins.status(DEPENDENCY) == "active"
        assert plugins._active[DEPENDENCY].instance.client.is_healthy
        assert plugins.status(PLUGIN) == "active"
    finally:
        if running is not None and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize(
    ("fault", "diagnostic"),
    [
        ("package", "immutable plugin store is corrupted"),
        ("configuration", "configuration revision is unavailable"),
        ("execution", "execution contract is unavailable"),
    ],
)
async def test_unavailable_original_contract_keeps_results_and_releases_candidates(
    tmp_path, fault, diagnostic
):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    calls = []
    runtime, plugins, service = host(tmp_path, store, calls)
    packages = configured_packages(tmp_path)
    try:
        await install_original(service, packages)
        pending = await runtime.run("deploy", driver_id=DRIVER)
        await upgrade(service, packages)
        original = pending.run.driver.implementation
        if fault == "package":
            source = (
                plugins.store.package_path(PLUGIN, original.release_id) / "review_driver_plugin.py"
            )
            source.write_text(
                source.read_text(encoding="utf-8") + "\n# corrupted\n", encoding="utf-8"
            )
        elif fault == "configuration":
            await service.configure(
                service.configuration(PLUGIN, package_digest=original.release_id).candidate(
                    values={"label": "changed"}
                )
            )

            def lose_revision(state):
                state.plugins[PLUGIN].activation.configuration_revisions.pop(
                    original.configuration_revision_id
                )
                return state

            plugins.store.mutate_state(lose_revision)
        else:
            store.save_run(
                pending.run.model_copy(
                    update={
                        "driver": pending.run.driver.model_copy(
                            update={
                                "implementation": original.model_copy(
                                    update={"execution_contract_hash": "0" * 64}
                                )
                            }
                        )
                    }
                )
            )
        current = plugins.store.read_state()
        result = await approve(runtime, pending)
        assert result.run.status == "failed"
        assert diagnostic in result.run.error
        assert result.run.driver_checkpoint.state == "ready"
        assert len(calls) == 1
        assert plugins.store.read_state() == current
        assert not plugins._retired
        assert plugins.status(PLUGIN) == "active"
    finally:
        await runtime.close()
        await plugins.close()


async def replace_driver(service, package):
    set_driver_body(
        package,
        'async def review(context, resume):\n    return sdk.DriverFinished("replacement")\n',
    )
    manifest_path = package / ".ouro-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "0.1.1"
    write_json(manifest_path, manifest)
    replacement = service.install(package)
    assert (await service.select(PLUGIN, replacement.package_digest)).status == "active"


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
async def test_upgrade_during_model_request_preserves_the_executing_driver(tmp_path, kind):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    provider = GateProvider()
    runtime.adapter.provider = provider
    package = plugin_source(tmp_path, kind)
    set_driver_body(
        package,
        """
async def review(context, resume):
    response = await context.models.request(tools=())
    return sdk.DriverFinished("original:" + response.text)
""",
    )
    service.install(package)
    running = None
    try:
        assert (await service.enable(PLUGIN)).status == "active"
        running = asyncio.create_task(runtime.run("work", driver_id=DRIVER))
        async with asyncio.timeout(5):
            await provider.first_started.wait()
        await replace_driver(service, package)
        assert plugins._retired
        provider.release_first.set()
        result = await running
        assert result.final_text == "original:first", result.run.error
        async with asyncio.timeout(5):
            while plugins._retired:
                await asyncio.sleep(0)
    finally:
        provider.release_first.set()
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)
        await runtime.close()
        await plugins.close()


async def test_retained_worker_is_released_at_each_wait_and_loaded_for_the_next_checkpoint(
    tmp_path,
):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    calls = []
    runtime, plugins, service = host(tmp_path, store, calls)
    package = plugin_source(tmp_path, "worker")
    set_driver_body(
        package,
        """
async def review(context, resume):
    step = resume.checkpoint if resume else 0
    if step == 2:
        return sdk.DriverFinished("done")
    result = await context.tools.execute((sdk.RunToolCall("Deploy"),), checkpoint=step + 1)
    assert result.suspended
    return sdk.DriverSuspended()
""",
    )
    service.install(package)
    try:
        assert (await service.enable(PLUGIN)).status == "active"
        first = await runtime.run("two steps", driver_id=DRIVER)
        assert first.run.status == "waiting_approval", first.run.error
        await replace_driver(service, package)
        second = await approve(runtime, first)
        assert second.run.status == "waiting_approval", second.run.error
        assert second.run.driver_checkpoint.payload == 2
        assert not plugins._retired
        assert len(calls) == 1
        finished = await approve(runtime, second)
        assert finished.final_text == "done", finished.run.error
        assert len(calls) == 2
        assert not plugins._retired
    finally:
        await runtime.close()
        await plugins.close()
