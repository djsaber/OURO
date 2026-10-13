import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, local

import pytest

from ouro.application.plugins import PluginService
from ouro.core.plugin_contracts import PluginImplementation
from ouro.core.run_drivers import RunDriverBinding
from ouro.core.state import RunRecord, SessionRecord
from ouro.plugins.installer import PluginInstaller
from ouro.plugins.store import PluginPackageStore
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from tests.plugins.test_plugin_system import _write_native_plugin

PLUGIN = "dev.ouro.echo"


@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
async def test_uninstall_retains_recoverable_code_without_install_or_activation(
    tmp_path, store_type
):
    source = tmp_path / "source"
    source.mkdir()
    _write_native_plugin(source)
    sessions = store_type(tmp_path / "sessions")
    sessions.initialize()
    packages = PluginPackageStore(tmp_path / "home")
    service = PluginService(packages, session_store=sessions)
    installed = service.install(source)
    session = SessionRecord()
    sessions.create_session(session)
    run = RunRecord(
        session_id=session.session_id,
        status="paused",
        driver=RunDriverBinding(
            driver_id=f"{PLUGIN}/run_drivers/reader",
            contract_hash="driver",
            implementation=PluginImplementation(
                plugin_id=PLUGIN,
                plugin_class="user",
                release_id=installed.package_digest,
                configuration_revision_id="configuration",
                execution_contract_hash="execution",
            ),
        ),
    )
    sessions.save_run(run)
    data = packages.data_root / PLUGIN
    data.mkdir(parents=True)
    (data / "owned").write_text("retained", encoding="utf-8")
    with pytest.raises(ValueError, match="active durable runs"):
        await service.remove(PLUGIN, purge_data=True)
    removed = await service.remove(PLUGIN)
    assert removed.retained_by == [f"{session.session_id}/{run.run_id}"]
    assert PLUGIN not in packages.read_state().plugins
    package = packages.package_path(PLUGIN, installed.package_digest)
    service = PluginService(PluginPackageStore(packages.home), session_store=sessions)
    assert service.gc() == []
    assert package.exists()
    with pytest.raises(ValueError, match="active durable runs"):
        service.purge_data(PLUGIN)
    manifest = source / ".ouro-plugin/plugin.json"
    updated = json.loads(manifest.read_text(encoding="utf-8"))
    updated["version"] = "0.2.0"
    manifest.write_text(json.dumps(updated), encoding="utf-8")
    replacement = service.install(source)
    with pytest.raises(ValueError, match="active durable runs"):
        await service.remove(PLUGIN, purge_data=True)
    await service.remove(PLUGIN)
    assert service.gc() == [str(packages.package_path(PLUGIN, replacement.package_digest))]
    sessions.save_run(run.model_copy(update={"status": "aborted"}))
    assert service.gc() == [str(package)]
    assert not package.exists()
    assert (data / "owned").read_text(encoding="utf-8") == "retained"


async def test_gc_preserves_code_while_another_host_still_owns_generation_data(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _write_native_plugin(source)
    service = PluginService(PluginPackageStore(tmp_path / "home"))
    installed = service.install(source)
    with service.store.data_lock(PLUGIN):
        await service.remove(PLUGIN)
        assert service.gc() == []
    assert service.gc() == [str(service.store.package_path(PLUGIN, installed.package_digest))]


def test_gc_cannot_delete_a_package_between_publication_and_install_commit(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _write_native_plugin(source)
    store = PluginPackageStore(tmp_path / "home")
    installer = PluginInstaller(store)
    entered, release = Event(), Event()
    record = installer._record_install

    def delayed(installed, configuration):
        entered.set()
        assert release.wait(5)
        return record(installed, configuration)

    monkeypatch.setattr(installer, "_record_install", delayed)
    with ThreadPoolExecutor(max_workers=2) as executor:
        installing = executor.submit(installer.install, source)
        try:
            assert entered.wait(5)
            collecting = executor.submit(store.garbage_collect, set())
            with pytest.raises(TimeoutError):
                collecting.result(timeout=0.1)
        finally:
            release.set()
        installed = installing.result(timeout=5)
        assert collecting.result(timeout=5) == []
    assert store.verify_package(PLUGIN, installed.installed.package_digest).exists()


def test_concurrent_gc_collects_each_uninstalled_package_once(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _write_native_plugin(source)
    store = PluginPackageStore(tmp_path / "home")
    installer = PluginInstaller(store)
    installed = installer.install(source).installed
    installer.remove_install(PLUGIN)
    rendezvous, thread = Barrier(2), local()
    lock = store.plugin_lock

    def synchronize(plugin_id):
        if not getattr(thread, "arrived", False):
            thread.arrived = True
            rendezvous.wait(timeout=5)
        return lock(plugin_id)

    monkeypatch.setattr(store, "plugin_lock", synchronize)
    with ThreadPoolExecutor(max_workers=2) as executor:
        collecting = [executor.submit(store.garbage_collect, set()) for _ in range(2)]
        removed = [path for task in collecting for path in task.result(timeout=5)]
    assert removed == [store.package_path(PLUGIN, installed.package_digest)]
