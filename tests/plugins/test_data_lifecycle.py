import asyncio
import sys

import pytest

from ouro.application.plugins import PluginService
from ouro.core.plugin_contracts import PluginImplementation
from ouro.core.run_drivers import RunDriverBinding
from ouro.core.state import RunRecord, SessionRecord
from ouro.plugins.models import BuiltinPluginActivationOverride
from ouro.plugins.store import PluginPackageStore
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from tests.plugins.test_lifecycle import PLUGIN_ID, LifecyclePlugin, create_runtime


async def test_purge_waits_for_every_host_to_release_plugin_resources(tmp_path):
    first = create_runtime(tmp_path, LifecyclePlugin)
    second = create_runtime(tmp_path, LifecyclePlugin)
    service = PluginService(first.store, builtin_plugins=first.builtin_plugins)
    try:
        assert await first.enable(PLUGIN_ID) == "active"
        await second.start()
        data = first.store.data_root / PLUGIN_ID / "records.json"
        data.parent.mkdir(parents=True, exist_ok=True)
        data.write_text("owned data", encoding="utf-8")
        await first.disable(PLUGIN_ID)
        assert second.has_resources(PLUGIN_ID)
        with pytest.raises(RuntimeError, match="in use"):
            service.purge_data(PLUGIN_ID)
        assert data.read_text(encoding="utf-8") == "owned data"
        await second.synchronize()
        await second.wait_stopped(PLUGIN_ID)
        service.purge_data(PLUGIN_ID)
        assert not data.exists()
    finally:
        await first.close()
        await second.close()


async def test_maintenance_blocks_enablement_without_changing_disabled_intent(tmp_path):
    runtime = create_runtime(tmp_path, LifecyclePlugin)
    try:
        before = runtime.store.read_state()
        with runtime.store.data_lock(PLUGIN_ID, exclusive=True):
            assert await runtime.enable(PLUGIN_ID) == "failed"
            assert runtime.store.read_state() == before
            assert not runtime.has_resources(PLUGIN_ID)

            def enable(state):
                state.builtin_activation_overrides[PLUGIN_ID] = BuiltinPluginActivationOverride(
                    desired_enabled=True
                )
                return state

            with pytest.raises(RuntimeError, match="in use"):
                runtime.store.mutate_state(enable)
            assert runtime.store.read_state() == before
        assert await runtime.enable(PLUGIN_ID) == "active"
    finally:
        await runtime.close()


async def test_failed_stop_keeps_data_reserved_until_cleanup_succeeds(tmp_path):
    allow_stop = False

    class Plugin(LifecyclePlugin):
        async def stop(self, reason, deadline):
            if not allow_stop:
                raise RuntimeError("resource is still open")

    runtime = create_runtime(tmp_path, Plugin)
    service = PluginService(runtime.store, builtin_plugins=runtime.builtin_plugins)
    try:
        assert await runtime.enable(PLUGIN_ID) == "active"
        assert await runtime.disable(PLUGIN_ID) == "failed"
        with pytest.raises(RuntimeError, match="in use"):
            service.purge_data(PLUGIN_ID)
        allow_stop = True
        await runtime.disable(PLUGIN_ID)
        service.purge_data(PLUGIN_ID)
    finally:
        allow_stop = True
        await runtime.close()


@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
async def test_uninstalled_data_remains_protected_by_resumable_run(tmp_path, store_type):
    sessions = store_type(
        tmp_path / ("sessions.sqlite3" if store_type is SQLiteSessionStore else "sessions")
    )
    sessions.initialize()
    session = SessionRecord()
    sessions.create_session(session)
    run = RunRecord(
        session_id=session.session_id,
        status="paused",
        driver=RunDriverBinding(
            driver_id=f"{PLUGIN_ID}/run_drivers/reader",
            contract_hash="driver",
            implementation=PluginImplementation(
                plugin_id=PLUGIN_ID,
                plugin_class="user",
                release_id="release",
                configuration_revision_id="configuration",
                execution_contract_hash="execution",
            ),
        ),
    )
    sessions.save_run(run)
    service = PluginService(PluginPackageStore(tmp_path / "home"), session_store=sessions)
    data = service.store.data_root / PLUGIN_ID / "records.json"
    data.parent.mkdir(parents=True)
    data.write_text("private data", encoding="utf-8")
    with pytest.raises(ValueError, match="active durable runs"):
        service.purge_data(PLUGIN_ID)
    with pytest.raises(ValueError, match="active durable runs"):
        await service.maintain_data(PLUGIN_ID, [sys.executable, "-c", "print('unexpected')"])
    assert data.read_text(encoding="utf-8") == "private data"
    sessions.save_run(run.model_copy(update={"status": "aborted"}))
    service.purge_data(PLUGIN_ID)
    assert not data.exists()
    assert sessions.load_run(session.session_id, run.run_id).status == "aborted"
    assert service.store.audit_events()[-1].action == "purge_data"


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout", "cancel"])
async def test_maintenance_owns_data_until_command_settles(tmp_path, outcome):
    runtime = create_runtime(tmp_path, LifecyclePlugin)
    service = PluginService(runtime.store, builtin_plugins=runtime.builtin_plugins)
    data = runtime.store.data_root / PLUGIN_ID
    data.mkdir(parents=True)
    original = data / "records.json"
    original.write_bytes(b"owned data")
    command = (
        "from pathlib import Path; import time, sys; "
        "Path('ready').touch(); "
        "\nwhile not Path('finish').exists(): time.sleep(0.01)\n"
        f"sys.exit({7 if outcome == 'failure' else 0})"
    )
    pending = asyncio.create_task(
        service.maintain_data(
            PLUGIN_ID,
            [sys.executable, "-c", command],
            timeout_seconds=1 if outcome == "timeout" else 10,
        )
    )
    try:
        async with asyncio.timeout(5):
            while not (data / "ready").exists():
                await asyncio.sleep(0.01)
        with pytest.raises(RuntimeError, match="in use"):
            service.purge_data(PLUGIN_ID)
        assert await runtime.enable(PLUGIN_ID) == "failed"
        if outcome == "cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            if outcome != "timeout":
                (data / "finish").touch()
            result = await pending
            assert result.status == ("succeeded" if outcome == "success" else "failed")
            assert result.timed_out == (outcome == "timeout")
        assert original.read_bytes() == b"owned data"
        assert not runtime.store.read_state().builtin_activation_overrides
        assert runtime.store.audit_events()[-1].action == "maintain_data"
        assert runtime.store.audit_events()[-1].result == (
            "succeeded" if outcome == "success" else "failed"
        )
        with runtime.store.data_lock(PLUGIN_ID, exclusive=True):
            pass
        assert await runtime.enable(PLUGIN_ID) == "active"
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await runtime.close()
