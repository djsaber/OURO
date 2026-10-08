from __future__ import annotations

import asyncio
import gc
import subprocess
import sys
import textwrap
import weakref
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ouro.application.runs import reserve_run
from ouro.application.runtime import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import AgentCatalogSnapshot, DelegatedConstraints
from ouro.core.observers import ContextPreparedObservation
from ouro.core.provider import ModelMetadata
from ouro.core.session_controls import HostSessionInput
from ouro.core.state import RunRecord
from ouro.plugins.catalog import ExtensionCatalogBase, build_extension_catalog
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry


def test_headless_runtime_completes_and_closes_with_tui_imports_blocked():
    # A fresh process catches transitive Host dependencies too; the test runner
    # may already have imported Textual while collecting other integration tests.
    script = textwrap.dedent(
        """
        import asyncio
        import importlib.abc
        import sys
        import tempfile
        from pathlib import Path

        class NoTuiImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'textual' or fullname.startswith(('textual.', 'ouro.tui')):
                    raise ImportError(f'Runtime attempted a TUI dependency: {fullname}')
                return None

        sys.meta_path.insert(0, NoTuiImports())
        from ouro.application.runtime import RuntimeService
        from ouro.core.adapter import RuntimeAdapter
        from ouro.core.provider import ModelMetadata
        from ouro.providers import FauxProvider
        from ouro.sessions import LocalSessionStore, SQLiteSessionStore
        from ouro.tools import ToolRegistry

        async def main():
            with tempfile.TemporaryDirectory(prefix='ouro-headless-') as directory:
                root = Path(directory)
                for store in (LocalSessionStore(root / 'files'),
                              SQLiteSessionStore(root / 'sqlite' / 'sessions.db')):
                    store.initialize()
                    async with RuntimeService(
                        store=store, tools=ToolRegistry(),
                        adapter=RuntimeAdapter(
                            FauxProvider(), ModelMetadata(model_id='faux', provider_id='faux')
                        ),
                    ) as runtime:
                        result = await runtime.run('hello')
                        assert result.run.status == 'completed'
                        await runtime.drain_events()

        asyncio.run(main())
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
async def runtime(tmp_path):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    app = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="faux", provider_id="faux")),
    )
    app.create_session(_session_id="session", title="Resource ownership")
    catalog = build_extension_catalog(
        ExtensionCatalogBase.capture(
            tools=app.tools,
            observers=app.observers,
            commands=None,
            compaction=None,
            continuations=None,
            tool_policies=None,
        ),
        (),
    )

    source = SimpleNamespace(snapshot=catalog.acquire)
    yield app, catalog, source
    # Explicit close tests own their shutdown result, including expected errors.
    # Finish unstarted or pending shutdowns without replaying an observed failure.
    if app._close_task is None or not app._close_task.done():
        await app.close()


def delegated_run(session_id="session", **updates):
    return RunRecord(
        session_id=session_id,
        status="waiting_approval",
        agent_catalog=AgentCatalogSnapshot(),
        metadata={
            "effective_tool_names": [],
            "effective_tool_refs": [],
            "delegated_constraints": {"allow_external_paths": False},
        },
        **updates,
    )


@pytest.mark.parametrize("extensions", [False, True])
async def test_close_releases_all_run_references_with_or_without_catalog(runtime, extensions):
    app, catalog, source = runtime
    if extensions:
        app.bind_extensions(catalog=source)
    run = delegated_run()
    catalog_reference = weakref.ref(run.agent_catalog)
    await app._ensure_extension_snapshot(run)
    coordinator_reference = weakref.ref(app._tool_calls_for_run(run.run_id))
    assert coordinator_reference().execution_config.workspace_only
    del run

    await app.close()
    await app.close()
    gc.collect()
    assert catalog.lease_count == 0
    assert coordinator_reference() is None
    assert catalog_reference() is None


@pytest.mark.parametrize("status", ["waiting_approval", "waiting_user", "waiting_child"])
async def test_wait_releases_resources_and_resume_acquires_current_binding(runtime, status):
    app, catalog, source = runtime
    app.bind_extensions(catalog=source)
    run = delegated_run().model_copy(update={"status": status})
    await app._ensure_extension_snapshot(run)
    reference = weakref.ref(app._tool_calls_for_run(run.run_id))
    app._finish_run_observers(run)
    gc.collect()
    assert catalog.lease_count == 0
    assert reference() is None
    app.replace_tool_permission_overrides({"example": "deny"})
    await app._ensure_extension_snapshot(run)
    assert app._tool_calls_for_run(run.run_id).executor.permission_overrides["example"] == "deny"
    app._finish_run_observers(run.model_copy(update={"status": "completed"}))
    assert catalog.lease_count == 0


async def test_terminal_cleanup_keeps_independently_forked_observer_lease(runtime):
    app, catalog, source = runtime
    app.bind_extensions(catalog=source)
    run = delegated_run()
    await app._ensure_extension_snapshot(run)
    _, close_observer = app._observers_for_observation(
        ContextPreparedObservation(
            session_id=run.session_id,
            run_id=run.run_id,
            message_count=0,
            tool_count=0,
            model_round=0,
        )
    )
    assert catalog.lease_count == 2
    app._finish_run_observers(run.model_copy(update={"status": "completed"}))
    assert catalog.lease_count == 1
    close_observer()
    assert catalog.lease_count == 0


async def test_recovery_does_not_interpret_historical_control_metadata(runtime):
    app, catalog, source = runtime
    app.bind_extensions(catalog=source)
    run = delegated_run()
    run.metadata["session_control_snapshot"] = {"activations": [{"controller_id": "obsolete"}]}
    await app._ensure_extension_snapshot(run)
    assert app._active_controller_ids_for_run(run.run_id) == set()
    assert catalog.lease_count == 1


async def test_invalid_delegated_binding_does_not_publish_partial_tool_cap(runtime):
    app, _, _ = runtime
    run = delegated_run()
    await app._ensure_extension_snapshot(run)
    run.metadata["effective_tool_names"] = ["missing"]
    run.metadata["effective_tool_refs"] = "invalid"
    with pytest.raises(ValueError, match="effective tool references"):
        await app._ensure_extension_snapshot(run)
    # The original empty cap remains usable; no partially restored name is exposed.
    assert app._tools_for_run(run.run_id).definitions() == []


async def test_cancelled_binding_releases_owned_lease(runtime, monkeypatch):
    app, catalog, source = runtime
    app.bind_extensions(catalog=source)
    session = app.create_session(title="Cancelled binding")
    run = delegated_run(session.session_id)
    entered = asyncio.Event()

    async def hold(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr("ouro.application.runtime.run_blocking", hold)
    pending = asyncio.create_task(app._attach_extension_snapshot(run))
    await asyncio.wait_for(entered.wait(), 2)
    assert catalog.lease_count == 1
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert catalog.lease_count == 0


async def test_detaching_run_transfers_only_catalog_ownership(runtime):
    app, catalog, source = runtime
    app.bind_extensions(catalog=source)
    run = delegated_run()
    await app._ensure_extension_snapshot(run)
    reference = weakref.ref(app._tool_calls_for_run(run.run_id))
    snapshot = app._detach_extension_snapshot(run.run_id)
    gc.collect()
    assert reference() is None
    assert catalog.lease_count == 1
    await app.close()
    assert catalog.lease_count == 1
    snapshot.close()
    assert catalog.lease_count == 0


async def test_prepared_run_decodes_delegated_binding_once(runtime):
    app, catalog, source = runtime
    app.bind_extensions(catalog=source)
    session = app.create_session(title="Prepared binding")
    run = delegated_run(session.session_id).model_copy(update={"status": "queued"})
    run.metadata["extension_generations"] = []
    reserve_run(app.store, run)
    with patch.object(
        DelegatedConstraints, "model_validate", wraps=DelegatedConstraints.model_validate
    ) as decode:
        result = await app.run_prepared(
            run, HostSessionInput(prompt="hello", host_source_id="test")
        )
    assert result.run.status == "completed"
    assert decode.call_count == 1
    assert catalog.lease_count == 0


async def test_failed_recovery_does_not_retain_the_runs_agent_catalog(runtime):
    app, catalog, source = runtime
    app.bind_extensions(catalog=source)

    def unavailable():
        raise RuntimeError("catalog unavailable")

    source.snapshot = unavailable
    run = delegated_run()
    reference = weakref.ref(run.agent_catalog)
    with pytest.raises(RuntimeError, match="catalog unavailable"):
        await app._ensure_extension_snapshot(run)
    del run
    gc.collect()
    assert reference() is None
    assert catalog.lease_count == 0


async def test_close_finishes_other_resources_when_a_lease_callback_fails(runtime):
    app, catalog, source = runtime
    app.bind_extensions(catalog=source)

    def fail(_catalog):
        raise ValueError("release callback failed")

    source.snapshot = lambda: catalog.acquire(on_close=fail)
    run = delegated_run()
    await app._ensure_extension_snapshot(run)
    reference = weakref.ref(app._tool_calls_for_run(run.run_id))
    source.snapshot = catalog.acquire
    await app._ensure_extension_snapshot(delegated_run())
    with patch.object(app.models, "close", wraps=app.models.close) as close_models:
        with pytest.raises(ExceptionGroup, match="runtime shutdown failed") as failure:
            await app.close()
        assert len(failure.value.exceptions) == 1
        assert str(failure.value.exceptions[0]) == "release callback failed"
        with pytest.raises(ExceptionGroup, match="runtime shutdown failed") as repeated:
            await app.close()
        assert repeated.value is failure.value
        close_models.assert_awaited_once()
    gc.collect()
    assert catalog.lease_count == 0
    assert reference() is None
