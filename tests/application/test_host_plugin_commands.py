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

from ouro.application.plugins.commands import (
    AmbiguousPluginCommandError,
    consume_plugin_command,
    submit_plugin_command,
)
from ouro.application.plugins.service import PluginService
from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import PersistenceError
from ouro.core.messages import MessageRecord
from ouro.core.observers import ObserverRegistry
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.session_commits import SessionCommit, SessionCommitConflict, SessionCommitStep
from ouro.core.session_controls import SessionInputBatchRequest, SessionInputRequest
from ouro.core.state import RunRecord
from ouro.plugins.catalog.snapshot import ExtensionCatalogBase, build_extension_catalog
from ouro.plugins.installation.store import PluginPackageStore
from ouro.plugins.registry import (
    PluginCommand,
    PluginCommandError,
    PluginCommandRegistry,
    PluginCommandResult,
)
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from tests.event_capture import recorded_events


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

        def current_catalog():
            bindings.append(catalog.catalog_id)
            return catalog

        async def synchronize():
            pass

        return PluginService(
            PluginPackageStore(tmp_path),
            runtime=SimpleNamespace(
                builtin_contracts={},
                catalog=current_catalog,
                synchronize=synchronize,
                select_driver=catalog.driver_binding,
            ),
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


async def test_stop_command_commits_abort_intent_and_settles_execution(system, monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def stream(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
            yield ProviderStreamEvent(type="done")
        finally:
            stopped.set()

    monkeypatch.setattr(system.runtime.adapter.provider, "stream", stream)
    session = system.runtime.create_session()
    service = system.plugins(
        handler=lambda call: PluginCommandResult(command_id=call.command_id, run_action="stop")
    )
    task = asyncio.create_task(system.runtime.run("retained input", session_id=session.session_id))
    try:
        await asyncio.wait_for(started.wait(), 5)
        before = system.store.load_session(session.session_id).messages
        submitted = await asyncio.wait_for(
            submit_plugin_command(
                service, system.runtime, "example", "", session_id=session.session_id
            ),
            5,
        )
        assert stopped.is_set()
        result = await task
        assert submitted.execution.stop_requested_run_id == result.run.run_id
        assert result.run.status == "aborted"
        events = recorded_events(session.session_id)
        abort_events = [
            event.event_type for event in events if event.event_type.startswith("run_abort")
        ]
        assert abort_events == ["run_abort_requested", "run_aborted"]
        assert system.store.load_session(session.session_id).messages == before
        assert [item async for item in consume_plugin_command(system.runtime, submitted)] == []
    finally:
        if not task.done():
            await system.runtime.abort_run(session_id=session.session_id)
        await task


async def test_failed_stop_command_does_not_publish_abort_intent(system, monkeypatch):
    session = system.runtime.create_session()
    run = RunRecord(session_id=session.session_id, status="running")
    system.store.save_run(run)
    system.store.save_session(
        session.model_copy(update={"active_run_id": run.run_id, "status": "running"})
    )
    service = system.plugins(
        handler=lambda call: PluginCommandResult(command_id=call.command_id, run_action="stop")
    )

    def fail(request):
        assert request.runs[0].abort_requested
        raise PersistenceError("injected failure")

    monkeypatch.setattr(system.store, "commit_session", fail)
    with pytest.raises(PersistenceError, match="injected failure"):
        await service.execute_command("dev.example:example", session_id=session.session_id)
    assert not system.store.load_run(session.session_id, run.run_id).abort_requested
    events = recorded_events(session.session_id)
    assert not any(event.event_type == "run_abort_requested" for event in events)


async def test_delayed_stop_delivery_does_not_cancel_resumed_execution(system, monkeypatch):
    first_started = asyncio.Event()
    resumed_started = asyncio.Event()
    deliver_command = asyncio.Event()
    finish = asyncio.Event()
    command_committed = asyncio.Event()

    async def stream(**kwargs):
        (resumed_started if first_started.is_set() else first_started).set()
        await finish.wait()
        yield ProviderStreamEvent(type="text_delta", text="Done.")
        yield ProviderStreamEvent(type="done")

    monkeypatch.setattr(system.runtime.adapter.provider, "stream", stream)
    service = system.plugins(
        handler=lambda call: PluginCommandResult(command_id=call.command_id, run_action="stop")
    )
    execute = service.execute_command

    async def delayed_delivery(*args, **kwargs):
        result = await execute(*args, **kwargs)
        command_committed.set()
        await deliver_command.wait()
        return result

    monkeypatch.setattr(service, "execute_command", delayed_delivery)
    session = system.runtime.create_session()
    initial = asyncio.create_task(
        system.runtime.run("retained input", session_id=session.session_id)
    )
    command = resumed = None
    try:
        await asyncio.wait_for(first_started.wait(), 5)
        command = asyncio.create_task(
            submit_plugin_command(
                service, system.runtime, "example", "", session_id=session.session_id
            )
        )
        await asyncio.wait_for(command_committed.wait(), 5)
        stopped = await asyncio.wait_for(initial, 5)
        assert stopped.run.status == "aborted"
        resumed = asyncio.create_task(system.runtime.continue_run(session_id=session.session_id))
        await asyncio.wait_for(resumed_started.wait(), 5)
        deliver_command.set()
        await asyncio.wait_for(command, 5)
        assert not system.store.load_run(session.session_id, stopped.run.run_id).abort_requested
        finish.set()
        assert (await asyncio.wait_for(resumed, 5)).run.status == "completed"
    finally:
        deliver_command.set()
        finish.set()
        await asyncio.gather(*(task for task in (initial, command, resumed) if task is not None))


@pytest.mark.parametrize("problem", ["permission", "session", "contract", "inputs"])
async def test_run_action_requires_execution_authority_and_cannot_add_inputs(problem):
    registry = PluginCommandRegistry()
    registry.register(
        PluginCommand(
            name="control",
            plugin_id="dev.example",
            allow_session_inputs=problem != "permission",
            generation_id=None if problem == "contract" else "generation",
            execution_contract_hash="contract",
            handler=lambda call: PluginCommandResult(
                command_id=call.command_id,
                run_action="continue",
                session_inputs=[SessionInputRequest(prompt="new input")]
                if problem == "inputs"
                else [],
            ),
        )
    )
    with pytest.raises(PluginCommandError):
        await registry.execute(
            "dev.example:control", session_id=None if problem == "session" else "session"
        )


@pytest.mark.parametrize("superseded", [False, True])
async def test_continuation_command_is_consumed_once_and_stop_revokes_it(system, superseded):
    initial = await system.runtime.run("retained input")
    service = system.plugins(
        handler=lambda call: PluginCommandResult(
            command_id=call.command_id, run_action=call.argument
        )
    )
    submission = await submit_plugin_command(
        service, system.runtime, "example", "continue", session_id=initial.session.session_id
    )
    if superseded:
        await submit_plugin_command(
            service, system.runtime, "example", "stop", session_id=initial.session.session_id
        )
    submission = replace(
        submission,
        execution=type(submission.execution).model_validate_json(
            submission.execution.model_dump_json()
        ),
    )
    for _ in range(2):
        results = [result async for result in consume_plugin_command(system.runtime, submission)]
        assert not superseded or results == []
    assert len(system.store.load_runs(initial.session.session_id)) == (1 if superseded else 2)


@pytest.mark.parametrize("queued", [False, True])
async def test_stop_revokes_continuation_at_the_execution_commit(system, monkeypatch, queued):
    initial = await system.runtime.run("retained input")
    session_id = initial.session.session_id
    if queued:
        await submit_plugin_command(
            system.plugins("single"), system.runtime, "example", "", session_id=session_id
        )
    service = system.plugins(
        handler=lambda call: PluginCommandResult(
            command_id=call.command_id, run_action=call.argument
        )
    )
    submission = await submit_plugin_command(
        service, system.runtime, "example", "continue", session_id=session_id
    )
    entered, release = threading.Event(), threading.Event()
    start = system.runtime.run_lifecycle.start

    def delayed_start(**kwargs):
        # The queue path already holds the mutation lock here; delay before that instead.
        if not queued:
            entered.set()
            assert release.wait(5)
        return start(**kwargs)

    start_queued = system.runtime._start_queued_run

    def delayed_queued_start(**kwargs):
        entered.set()
        assert release.wait(5)
        return start_queued(**kwargs)

    monkeypatch.setattr(system.runtime.run_lifecycle, "start", delayed_start)
    monkeypatch.setattr(system.runtime, "_start_queued_run", delayed_queued_start)

    async def consume():
        return [result async for result in consume_plugin_command(system.runtime, submission)]

    pending = asyncio.create_task(consume())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await submit_plugin_command(
            service, system.runtime, "example", "stop", session_id=session_id
        )
    finally:
        release.set()
    assert await pending == []
    assert len(system.store.load_runs(session_id)) == 1
    session = system.store.load_session_record(session_id)
    assert session.continuation_request is None
    assert len(system.runtime.queued_inputs.eligible_session(session)) == int(queued)


async def test_conflicting_stop_preserves_the_accepted_continuation(system):
    initial = await system.runtime.run("retained input")
    session_id = initial.session.session_id
    entered, release = asyncio.Event(), asyncio.Event()

    async def command(call):
        if call.argument == "stop":
            entered.set()
            await release.wait()
        return PluginCommandResult(command_id=call.command_id, run_action=call.argument)

    service = system.plugins(handler=command)
    stopping = asyncio.create_task(
        submit_plugin_command(service, system.runtime, "example", "stop", session_id=session_id)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        submission = await submit_plugin_command(
            service, system.runtime, "example", "continue", session_id=session_id
        )
    finally:
        release.set()
        with pytest.raises(SessionCommitConflict):
            await stopping
    assert (
        system.store.load_session_record(session_id).continuation_request
        == submission.execution.continuation_request
    )
    results = [result async for result in consume_plugin_command(system.runtime, submission)]
    assert results[-1].run.status == "completed"
    assert len(system.store.load_runs(session_id)) == 2


async def test_withdrawn_history_revokes_an_unconsumed_continuation(system):
    initial = await system.runtime.run("retained input")
    session_id = initial.session.session_id
    service = system.plugins(
        handler=lambda call: PluginCommandResult(command_id=call.command_id, run_action="continue")
    )
    submission = await submit_plugin_command(
        service, system.runtime, "example", "", session_id=session_id
    )
    await system.runtime.undo(session_id)
    assert [result async for result in consume_plugin_command(system.runtime, submission)] == []
    assert system.store.load_session_record(session_id).continuation_request is None
    assert len(system.store.load_runs(session_id)) == 1


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
    current_catalog = service.runtime.catalog

    def history_changes():
        catalog = current_catalog()
        # Another owner withdraws the captured epoch while Catalog is acquired.
        if len(system.bindings) == 2:
            system.store.commit_session(
                SessionCommit(before=session, before_message_id=message.message_id)
            )
        return catalog

    service.runtime.catalog = history_changes
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
        from ouro.application.plugins.commands import consume_plugin_command, submit_plugin_command
        from ouro.application.runtime.service import RuntimeService
        from ouro.application.contracts.plugins import PluginCommandExecutionView, PluginCommandView
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
