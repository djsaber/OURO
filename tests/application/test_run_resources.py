from __future__ import annotations

import asyncio
import gc
import subprocess
import sys
import textwrap
import weakref
from collections import Counter, defaultdict
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ouro.application.inputs.requests import UserInputRequest
from ouro.application.runs.lifecycle import reserve_run
from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import AgentCatalogEntry, AgentCatalogSnapshot, AgentDefinition
from ouro.core.context_providers import ContextProviderRegistration, ContextProviderResult
from ouro.core.errors import PersistenceError
from ouro.core.observers import ContextPreparedObservation, ObserverRegistry
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.session_controls import HostSessionInput, SessionControlState
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.system_prompts import SystemPromptFragment, SystemPromptRegistration
from ouro.plugins.catalog.snapshot import ExtensionCatalogBase, build_extension_catalog
from ouro.providers import FauxProvider
from ouro.sessions.backends.files.store import LocalSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult
from tests.agent_facts import seed_delegation
from tests.application.test_run_continuations import _activation, _session_controller_registry


async def test_decision_tools_are_reused_and_rebound_with_their_catalog(
    runtime, tmp_path, monkeypatch
):
    import ouro.tools.registry as registries
    from ouro.local_application import LocalApplication

    app, _, _ = runtime
    async with LocalApplication.from_runtime(app, home=tmp_path):
        checked = []
        validate = registries.validate_tool_definition

        def record_validation(definition):
            checked.append(definition.name)
            validate(definition)

        monkeypatch.setattr(registries, "validate_tool_definition", record_validation)

        def catalog(agent_id):
            return AgentCatalogSnapshot(
                entries=(
                    AgentCatalogEntry(
                        definition=AgentDefinition(
                            agent_id=agent_id, name=agent_id, description="Read evidence"
                        )
                    ),
                )
            )

        run = RunRecord(
            session_id="session", status="waiting_approval", agent_catalog=catalog("first")
        )
        await app._ensure_extension_snapshot(run)
        first = app._tools_for_run(run.run_id).get("SpawnAgent").definition
        for _ in range(5):
            reloaded = RunRecord.model_validate_json(run.model_dump_json())
            await app._ensure_extension_snapshot(reloaded)
            assert app._tools_for_run(run.run_id).get("SpawnAgent").definition is first
        assert checked.count("SpawnAgent") == 1
        assert checked.count("SendAgentMessage") == 1
        assert checked.count("InterruptAgent") == 1
        changed = run.model_copy(update={"agent_catalog": catalog("second")})
        await app._ensure_extension_snapshot(changed)
        second = app._tools_for_run(run.run_id).get("SpawnAgent").definition
        assert second is not first
        assert first.parameters_schema["properties"]["agent_id"]["oneOf"][0]["const"] == "first"
        assert second.parameters_schema["properties"]["agent_id"]["oneOf"][0]["const"] == "second"
        app._finish_run_observers(changed)
        assert run.run_id not in app._run_resources
        await app._ensure_extension_snapshot(changed)
        assert app._tools_for_run(run.run_id).get("SpawnAgent").definition is not second


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
        from ouro.application.runtime.service import RuntimeService
        from ouro.core.adapter import RuntimeAdapter
        from ouro.core.provider import ModelMetadata
        from ouro.providers import FauxProvider
        from ouro.sessions.backends.files.store import LocalSessionStore
        from ouro.sessions.backends.sqlite.store import SQLiteSessionStore
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

    source = SimpleNamespace(
        snapshot=catalog.acquire,
        bind_observers=catalog.bind_observers,
        catalog=lambda: catalog,
        synchronize=AsyncMock(),
        driver_scope=nullcontext,
    )
    yield app, catalog, source
    if app._close_task is None or not app._close_task.done():
        await app.close()


async def test_empty_bound_provider_catalog_owns_the_request(runtime):
    app, catalog, source = runtime
    calls = []

    def prompt(_):
        calls.append("prompt")
        return SystemPromptFragment(text="base prompt")

    def observe(_):
        calls.append("observe")
        return ContextProviderResult()

    app.system_prompt_providers.replace_owner(
        "base", (SystemPromptRegistration("base", "p", prompt),)
    )
    app.context_providers.replace_owner(
        "base", (ContextProviderRegistration("base", "o", observe),)
    )
    app.bind_extensions(catalog=source)
    with source.snapshot() as lease:
        assert lease.request_providers is catalog.request_providers
    result = await app.run("Use the bound catalog", session_id="session")
    assert result.run.status == "completed", result.run.error
    assert calls == []
    assert catalog.lease_count == 0


def delegated_run(app, session_id="session", **updates):
    session = app.store.load_session_record(session_id)
    relation = (
        seed_delegation(app.store, session)
        if session.session_kind == "primary"
        else app.store.load_delegation(session.root_session_id, session.delegation_id)
    )
    return RunRecord(
        session_id=session_id,
        status="waiting_approval",
        agent_catalog=AgentCatalogSnapshot(),
        parent_run_id=relation.parent_run_id,
        initiator_id=f"ouro.subagents/{relation.delegation_id}",
        metadata={
            "root_session_id": relation.root_session_id,
            "delegation_id": relation.delegation_id,
            "agent_id": relation.agent_id,
        },
        **updates,
    )


@pytest.mark.parametrize("extensions", [False, True])
async def test_close_releases_all_run_references_with_or_without_catalog(runtime, extensions):
    app, catalog, source = runtime
    if extensions:
        app.bind_extensions(catalog=source)
    run = delegated_run(app)
    catalog_reference = weakref.ref(run.agent_catalog)
    await app._ensure_extension_snapshot(run)
    coordinator_reference = weakref.ref(app._tool_calls_for_run(run.run_id))
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
    run = delegated_run(app).model_copy(update={"status": status})
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


async def test_unmatched_observation_does_not_retain_the_run_catalog(runtime):
    app, catalog, source = runtime
    app.bind_extensions(catalog=source)
    run = delegated_run(app)
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
    assert catalog.lease_count == 1
    app._finish_run_observers(run.model_copy(update={"status": "completed"}))
    assert catalog.lease_count == 0
    close_observer()
    assert catalog.lease_count == 0


async def test_recovery_does_not_interpret_historical_control_metadata(runtime):
    app, catalog, source = runtime
    app.bind_extensions(catalog=source)
    run = delegated_run(app)
    run.metadata["session_control_snapshot"] = {"activations": [{"controller_id": "obsolete"}]}
    await app._ensure_extension_snapshot(run)
    assert app._active_controller_ids_for_run(run.run_id) == set()
    assert catalog.lease_count == 1


async def test_invalid_delegated_binding_does_not_publish_partial_tool_cap(runtime):
    app, _, _ = runtime
    run = delegated_run(app)
    await app._ensure_extension_snapshot(run)
    run.metadata["agent_id"] = "different-agent"
    with pytest.raises(PersistenceError, match="child_delegation_mismatch"):
        await app._ensure_extension_snapshot(run)
    assert app._tools_for_run(run.run_id).definitions() == []


async def test_cancelled_binding_releases_owned_lease(runtime, monkeypatch):
    app, catalog, source = runtime
    app.bind_extensions(catalog=source)
    session = app.create_session(title="Cancelled binding")
    run = delegated_run(app, session.session_id)
    entered = asyncio.Event()

    async def hold(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr("ouro.application.runtime.service.run_blocking", hold)
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
    run = delegated_run(app)
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


async def test_prepared_run_loads_frozen_delegation_once(runtime):
    app, catalog, source = runtime
    app.bind_extensions(catalog=source)
    session = app.create_session(title="Prepared binding")
    run = delegated_run(app, session.session_id).model_copy(update={"status": "queued"})
    run.metadata["extension_generations"] = []
    reserve_run(app.store, run)
    with patch.object(app.store, "load_delegation", wraps=app.store.load_delegation) as load:
        result = await app.run_prepared(
            run, HostSessionInput(prompt="hello", host_source_id="test")
        )
    assert result.run.status == "completed"
    load.assert_called_once_with(run.metadata["root_session_id"], run.metadata["delegation_id"])
    assert catalog.lease_count == 0


async def test_failed_recovery_does_not_retain_the_runs_agent_catalog(runtime):
    app, catalog, source = runtime
    app.bind_extensions(catalog=source)

    def unavailable():
        raise RuntimeError("catalog unavailable")

    source.snapshot = unavailable
    run = delegated_run(app)
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
    run = delegated_run(app)
    await app._ensure_extension_snapshot(run)
    reference = weakref.ref(app._tool_calls_for_run(run.run_id))
    source.snapshot = catalog.acquire
    await app._ensure_extension_snapshot(delegated_run(app))
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


@pytest.mark.parametrize("commit_fails", [False, True])
async def test_queued_admission_retains_catalog_through_commit(runtime, monkeypatch, commit_fails):
    app, catalog, source = runtime
    app.bind_extensions(catalog=source)
    commit = app.store.commit_session
    accepted = []

    def commit_input(request):
        if request.inputs:
            assert catalog.lease_count == 1
            item = request.inputs[0].item
            assert item.prompt_policy_checked
            assert item.extension_catalog_id == catalog.catalog_id
            accepted.append(item.queue_id)
            if commit_fails:
                raise OSError("input commit failed")
        return commit(request)

    monkeypatch.setattr(app.store, "commit_session", commit_input)
    request = UserInputRequest(prompt="next input", session_id="session")
    if commit_fails:
        with pytest.raises(OSError, match="input commit failed"):
            await app.accept_user_input(request)
    else:
        receipt = await app.accept_user_input(request)
        assert receipt.queue_id == request.input_id
    assert accepted == [request.input_id]
    assert catalog.lease_count == 0
    assert not app._run_resources
    assert app.store.load_session_record("session").pending_input_ids == (
        () if commit_fails else (request.input_id,)
    )


@pytest.mark.parametrize("concurrent", [False, True])
async def test_root_runs_keep_their_budgets_and_tool_selections(tmp_path, concurrent):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    activation = _activation("dev.example.mode", "mode")
    barrier = asyncio.Barrier(2)
    requests = defaultdict(list)
    effects = Counter()

    class Provider:
        provider_id = "capture"

        async def stream(self, *, request_context, tools, **kwargs):
            session_id = request_context.values["session.id"]
            requests[session_id].append(sorted(tool["name"] for tool in tools))
            if len(requests[session_id]) == 1:
                if concurrent:
                    await asyncio.wait_for(barrier.wait(), timeout=15)
                yield ProviderStreamEvent.from_tool_call(
                    {"id": "read", "name": "Read", "arguments": {}}
                )
            else:
                yield ProviderStreamEvent(type="text_delta", text="done")
            yield ProviderStreamEvent(type="done")

    def read(invocation, context):
        effects[invocation.session_id] += 1
        return ToolResult(
            tool_call_id=invocation.tool_call_id, tool_name=invocation.tool_name, content="ok"
        )

    tools = ToolRegistry()
    for name, scope in (("Read", None), ("Selected", activation.controller_id)):
        tools.register(
            Tool(
                definition=ToolDefinition(
                    name=name,
                    description=name,
                    permission_mode="auto",
                    active_when_controller_id=scope,
                ),
                handler=read,
            )
        )
    catalog = build_extension_catalog(
        ExtensionCatalogBase.capture(
            tools=tools,
            observers=ObserverRegistry(),
            commands=None,
            continuations=None,
            compaction=None,
            tool_policies=None,
            session_controllers=_session_controller_registry(
                activation.plugin_id, activation.execution_contract_hash, []
            ),
        ),
        (),
    )
    async with RuntimeService(
        store=store,
        tools=tools,
        adapter=RuntimeAdapter(
            Provider(), ModelMetadata(model_id="capture", provider_id="capture")
        ),
    ) as runtime:
        runtime.bind_extensions(
            catalog=SimpleNamespace(
                snapshot=catalog.acquire,
                bind_observers=catalog.bind_observers,
                catalog=lambda: catalog,
                synchronize=AsyncMock(),
                driver_scope=nullcontext,
            )
        )

        async def drive(sid, rounds, controls):
            store.create_session(SessionRecord(session_id=sid, controls=controls))
            run = reserve_run(
                store,
                RunRecord(
                    session_id=sid,
                    status="queued",
                    metadata={
                        "run_budget": RunBudgetPolicy(max_model_rounds=rounds).model_dump(
                            mode="json"
                        )
                    },
                ),
            )
            result = await runtime.run_prepared(
                run, HostSessionInput(prompt="read", host_source_id="fixture")
            )
            return result.run.status, result.run.model_round_count, requests[sid], effects[sid]

        short = drive("short", 1, SessionControlState())
        long = drive("long", 2, SessionControlState(revision=1, activations=[activation]))
        results = await asyncio.gather(short, long) if concurrent else [await short, await long]
        assert results == [
            ("paused", 1, [["Read"]], 1),
            ("completed", 2, [["Read", "Selected"], ["Read", "Selected"]], 1),
        ]
    assert catalog.lease_count == 0
