from __future__ import annotations

import asyncio
import gc
import subprocess
import sys
import textwrap
import threading
import weakref
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ouro.application.plugin_commands import (
    AmbiguousPluginCommandError,
    consume_plugin_command,
    submit_plugin_command,
)
from ouro.application.plugins import PluginService
from ouro.application.runtime import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import PersistenceError
from ouro.core.messages import MessageRecord
from ouro.core.observers import ObserverRegistry
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.session_commits import SessionCommit, SessionCommitConflict, SessionCommitStep
from ouro.core.session_controls import SessionInputBatchRequest, SessionInputRequest
from ouro.core.state import RunRecord
from ouro.plugins.catalog import ExtensionCatalogBase, build_extension_catalog
from ouro.plugins.registry import (
    PluginCommand,
    PluginCommandError,
    PluginCommandRegistry,
    PluginCommandResult,
)
from ouro.plugins.store import PluginPackageStore
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry


@pytest.fixture(params=["files", "sqlite"])
async def system(tmp_path, request):
    store = (
        LocalSessionStore(tmp_path)
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    )
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(type="text_delta", text="done"),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path,
    )
    calls = []
    bindings = []

    def plugins(kind="query", *, requires_session=False, ambiguous=False, handler=None):
        def execute(call):
            calls.append(call)
            if handler is not None:
                return handler(call)
            items = [
                SessionInputRequest(
                    prompt="first prompt", role="user", scaffold=True, semantic_tag="setup"
                ),
                SessionInputRequest(prompt="second prompt", role="user"),
            ]
            return PluginCommandResult(
                command_id=call.command_id,
                output="accepted",
                session_inputs=items[:1]
                if kind == "single"
                else items
                if kind == "multiple"
                else [],
                session_input_batches=[SessionInputBatchRequest(inputs=tuple(items))]
                if kind == "batch"
                else [],
            )

        commands = PluginCommandRegistry()
        command = PluginCommand(
            name="example",
            handler=execute,
            plugin_id="dev.example",
            generation_id="generation",
            execution_contract_hash="c" * 64,
            allow_session_inputs=True,
            requires_session=requires_session,
        )
        commands.register(command)
        if ambiguous:
            commands.register(replace(command, plugin_id="dev.other"))
        catalog = build_extension_catalog(
            ExtensionCatalogBase.capture(
                tools=runtime.tools,
                observers=ObserverRegistry(),
                commands=commands,
                compaction=None,
                continuations=None,
                tool_policies=None,
            ),
            (),
        )

        def snapshot():
            bindings.append(catalog.catalog_id)
            return catalog.acquire()

        return PluginService(
            PluginPackageStore(tmp_path),
            runtime=SimpleNamespace(builtin_contracts={}, snapshot=snapshot),
            session_store=store,
        )

    yield SimpleNamespace(
        runtime=runtime, store=store, plugins=plugins, calls=calls, bindings=bindings
    )
    await runtime.close()


async def test_queries_and_unknown_or_ambiguous_names_do_not_create_sessions(system):
    service = system.plugins(ambiguous=True)
    assert (
        await submit_plugin_command(service, system.runtime, "missing", "", session_id=None) is None
    )
    with pytest.raises(
        AmbiguousPluginCommandError, match="/dev.example:example, /dev.other:example"
    ):
        await submit_plugin_command(service, system.runtime, "example", "", session_id=None)
    submitted = await submit_plugin_command(
        service, system.runtime, "dev.example:example", "", session_id=None
    )
    assert submitted.session_id is None and not submitted.has_inputs
    assert [result async for result in consume_plugin_command(system.runtime, submitted)] == []
    assert not system.runtime.sessions.catalog().entries
    assert len(system.calls) == 1


@pytest.mark.parametrize("kind", ["single", "multiple", "batch"])
@pytest.mark.parametrize("existing", [False, True])
async def test_command_inputs_keep_fifo_batch_boundaries_and_attribution(system, kind, existing):
    session_id = system.runtime.create_session(title="existing").session_id if existing else None
    submitted = await submit_plugin_command(
        system.plugins(kind),
        system.runtime,
        "example",
        "command title",
        session_id=session_id,
    )
    assert submitted.has_inputs
    assert bool(submitted.execution.accepted_inputs) == existing
    assert system.calls[0].session_id == session_id
    results = [result async for result in consume_plugin_command(system.runtime, submitted)]
    assert all(result.run.status == "completed" for result in results)
    snapshot = system.store.load_session(submitted.session_id)
    messages = [message for message in snapshot.messages if message.role == "user"]
    assert [message.text_content for message in messages] == (
        ["first prompt"] if kind == "single" else ["first prompt", "second prompt"]
    )
    assert len({message.run_id for message in messages}) == (2 if kind == "multiple" else 1)
    assert messages[0].scaffold and messages[0].semantic_tag == "setup"
    assert all(message.metadata["plugin_id"] == "dev.example" for message in messages)
    if existing:
        # Durable receipts can be consumed again without replaying the plugin or input.
        async for _result in consume_plugin_command(system.runtime, submitted):
            pass
        assert len(system.store.load_session(submitted.session_id).messages) == len(
            snapshot.messages
        )
        assert len(system.calls) == 1


async def test_required_session_is_reported_before_command_failure(system):
    seen = []

    def fail(call):
        assert seen == [call.session_id]
        raise ValueError("command failed")

    with pytest.raises(PluginCommandError, match="command failed"):
        await submit_plugin_command(
            system.plugins(requires_session=True, handler=fail),
            system.runtime,
            "example",
            " title  text ",
            session_id=None,
            on_session_created=seen.append,
            approval_mode="auto",
            interaction_mode="unattended",
        )
    session = system.store.load_session_record(seen[0])
    assert session.title == "title text"
    assert session.approval_mode == "auto" and session.interaction_mode == "unattended"
    assert not system.store.load_runs(session.session_id)


async def test_cancelled_session_creation_settles_and_reports_owned_identity(system, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    create = system.runtime.create_session
    seen = []

    def held_create(**kwargs):
        created = create(**kwargs)
        entered.set()
        assert release.wait(5)
        return created

    monkeypatch.setattr(system.runtime, "create_session", held_create)
    task = asyncio.create_task(
        submit_plugin_command(
            system.plugins(requires_session=True),
            system.runtime,
            "example",
            "",
            session_id=None,
            on_session_created=seen.append,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert system.store.load_session_record(seen[0]).session_id == seen[0]
    assert not system.calls


async def test_command_creation_and_catalog_query_do_not_call_store_on_event_loop(
    system, monkeypatch
):
    loop_thread = threading.get_ident()
    reads = []
    load = system.store.load_session_record
    create = system.store.create_session

    def checked_load(*args, **kwargs):
        reads.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return load(*args, **kwargs)

    def checked_create(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        return create(*args, **kwargs)

    monkeypatch.setattr(system.store, "load_session_record", checked_load)
    monkeypatch.setattr(system.store, "create_session", checked_create)
    submitted = await submit_plugin_command(
        system.plugins(requires_session=True),
        system.runtime,
        "example",
        "",
        session_id=None,
    )
    assert submitted.session_id is not None and reads


async def test_command_catalog_and_harness_share_one_session_read(system, monkeypatch):
    session = system.runtime.create_session(title="one context")
    record_reads = 0
    harness_reads = 0
    load_record = system.store.load_session_record
    load_harness = system.store.load_session_harness

    def record(*args, **kwargs):
        nonlocal record_reads
        record_reads += 1
        return load_record(*args, **kwargs)

    def harness(*args, **kwargs):
        nonlocal harness_reads
        harness_reads += 1
        return load_harness(*args, **kwargs)

    monkeypatch.setattr(system.store, "load_session_record", record)
    monkeypatch.setattr(system.store, "load_session_harness", harness)
    submitted = await submit_plugin_command(
        system.plugins(),
        system.runtime,
        "example",
        "",
        session_id=session.session_id,
    )
    assert submitted.execution.output == "accepted"
    assert harness_reads == 1
    # The combined Session/Harness read supplies Catalog selection too.
    assert record_reads == 0


async def test_active_run_command_uses_current_catalog(system):
    session = system.runtime.create_session(title="active")
    run = RunRecord(
        session_id=session.session_id,
        status="waiting_user",
        metadata={
            "extension_generations": [{"generation_id": "old"}],
            "extension_catalog_id": "old-catalog",
        },
    )
    system.store.save_run(run)
    system.store.save_session(session.model_copy(update={"active_run_id": run.run_id}))
    submitted = await submit_plugin_command(
        system.plugins("batch"),
        system.runtime,
        "example",
        "",
        session_id=session.session_id,
    )
    assert submitted.execution.accepted_inputs
    assert len(system.bindings) == 2
    assert len(set(system.bindings)) == 1
    assert "old-catalog" not in system.bindings
    assert not system.store.load_session(session.session_id).messages
    assert (
        len(
            system.runtime.queued_inputs.eligible_session(
                system.store.load_session_record(session.session_id)
            )
        )
        == 2
    )


async def test_command_does_not_recapture_withdrawn_history_after_catalog_acquisition(system):
    session = system.runtime.create_session(title="capture boundary")
    message = MessageRecord.text(session_id=session.session_id, role="user", text="withdraw me")
    committed = system.store.commit_session(
        SessionCommit(
            before=session,
            steps=(SessionCommitStep(message=message),),
        )
    )
    session = committed.session
    run = RunRecord(session_id=session.session_id, status="waiting_user")
    system.store.save_run(run)
    session = session.model_copy(update={"active_run_id": run.run_id})
    system.store.save_session(session)
    service = system.plugins("batch")
    snapshot = service.runtime.snapshot

    def history_changes():
        lease = snapshot()
        # Another owner withdraws the captured epoch while Catalog is acquired.
        if len(system.bindings) == 2:
            system.store.commit_session(
                SessionCommit(before=session, before_message_id=message.message_id)
            )
        return lease

    service.runtime.snapshot = history_changes
    with pytest.raises(SessionCommitConflict):
        await submit_plugin_command(
            service, system.runtime, "example", "", session_id=session.session_id
        )
    assert len(system.calls) == 1
    assert system.store.load_session_record(session.session_id).history_epoch == 1
    assert not system.runtime.queued_inputs.eligible_session(
        system.store.load_session_record(session.session_id)
    )


async def test_command_inputs_queue_behind_a_running_model(system, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_stream(**kwargs):
        entered.set()
        await release.wait()
        yield ProviderStreamEvent(type="text_delta", text="done")
        yield ProviderStreamEvent(type="done")

    monkeypatch.setattr(system.runtime.adapter.provider, "stream", held_stream)
    session = system.runtime.create_session(title="active")
    running = asyncio.create_task(system.runtime.run("start", session_id=session.session_id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        active_id = system.store.load_session_record(session.session_id).active_run_id
        submitted = await submit_plugin_command(
            system.plugins("batch"),
            system.runtime,
            "example",
            "",
            session_id=session.session_id,
        )
        results = [result async for result in consume_plugin_command(system.runtime, submitted)]
        assert [result.run.run_id for result in results] == [active_id]
        assert not running.done()
        assert len(system.store.load_runs(session.session_id)) == 1
    finally:
        release.set()
        await running


async def test_failed_acceptance_does_not_publish_receipts_or_retry_command(system, monkeypatch):
    session = system.runtime.create_session(title="target")

    def reject(request):
        raise PersistenceError("before decision")

    monkeypatch.setattr(system.store, "commit_session", reject)
    with pytest.raises(PersistenceError, match="before decision"):
        await submit_plugin_command(
            system.plugins("batch"),
            system.runtime,
            "example",
            "",
            session_id=session.session_id,
        )
    assert len(system.calls) == 1
    assert not system.runtime.queued_inputs.eligible_session(
        system.store.load_session_record(session.session_id)
    )


def test_plugin_command_application_runs_with_host_imports_blocked(tmp_path):
    script = textwrap.dedent("""
        import asyncio, importlib.abc, sys
        from pathlib import Path

        class BlockHosts(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "textual" or fullname.startswith(
                    ("textual.", "ouro.tui", "ouro.cli")
                ):
                    raise AssertionError(f"Host import: {fullname}")

        sys.meta_path.insert(0, BlockHosts())
        from ouro.application.plugin_commands import consume_plugin_command, submit_plugin_command
        from ouro.application.runtime import RuntimeService
        from ouro.application.plugin_models import PluginCommandExecutionView, PluginCommandView
        from ouro.core.adapter import RuntimeAdapter
        from ouro.core.provider import ModelMetadata, ProviderStreamEvent
        from ouro.providers import FauxProvider
        from ouro.sessions import LocalSessionStore, SQLiteSessionStore
        from ouro.tools import ToolRegistry

        class Commands:
            def list_commands(self):
                return [PluginCommandView(
                    command_id="test:query", plugin_id="test", name="query", requires_session=True,
                )]
            async def execute_command(self, command_id, argument, **kwargs):
                assert kwargs["session_id"] is not None
                return PluginCommandExecutionView(command_id=command_id, output="query response")

        async def main():
            root = Path(sys.argv[1])
            stores = (LocalSessionStore(root / "files"), SQLiteSessionStore(root / "sqlite.db"))
            for store in stores:
                store.initialize()
                runtime = RuntimeService(store=store, tools=ToolRegistry(), adapter=RuntimeAdapter(
                    FauxProvider([ProviderStreamEvent(type="done")]),
                    ModelMetadata(model_id="faux", provider_id="faux"),
                ))
                try:
                    result = await submit_plugin_command(
                        Commands(), runtime, "query", "", session_id=None,
                    )
                    assert result.session_id and result.execution.output == "query response"
                    assert [item async for item in consume_plugin_command(runtime, result)] == []
                finally:
                    await runtime.close()

        asyncio.run(main())
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_command_consumption_does_not_retain_prior_run_results(system, monkeypatch):
    submit = system.runtime.submit_plugin_input
    references = []
    retained = []

    async def observed_submit(*args, **kwargs):
        if len(references) == 2:
            gc.collect()
            retained.append(references[0]() is not None)
        result = await submit(*args, **kwargs)
        references.append(weakref.ref(result))
        return result

    def command(call):
        return PluginCommandResult(
            command_id=call.command_id,
            session_inputs=[SessionInputRequest(prompt=f"input {index}") for index in range(3)],
        )

    monkeypatch.setattr(system.runtime, "submit_plugin_input", observed_submit)
    submission = await submit_plugin_command(
        system.plugins(handler=command),
        system.runtime,
        "example",
        "run",
        session_id=None,
    )
    async for result in consume_plugin_command(system.runtime, submission):
        assert result.run.status == "completed"
    assert len(references) == 3
    assert retained == [False]
