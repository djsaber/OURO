from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from ouro.application.agents.recovery import AgentRecoveryService
from ouro.application.contracts.agents import SpawnAgentRequest
from ouro.core.errors import PersistenceError, SessionBusyError
from ouro.core.session_controls import HostSessionInput
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from tests.application.test_agent_control_cancellation import SteerProvider
from tests.application.test_agent_multi_host import application


def _competing_accept(directory, backend, root_id, delegation_id, start, observations):
    """A separate Host only accepts work; the parent later starts the one accepted Run."""
    app = application(directory, backend, "C:/PowerShell/pwsh.exe")
    relation = app.runtime.store.load_delegation(root_id, delegation_id)
    request = HostSessionInput(
        prompt="Concurrent follow-up",
        role="user",
        mode="follow_up",
        semantic_tag="subagent-message",
        host_source_id="test/competing-follow-up",
        queue_id="queued_competing_followup",
    )
    observations.put(("ready", None))
    if not start.wait(30):
        observations.put(("error", "acceptance was not released"))
        return
    try:
        accepted, run = app.agents._prepare_agent_follow_up(relation, session_input=request)
        observations.put(("accepted", (accepted.current_child_run_id, run.run_id)))
    except Exception as exc:
        observations.put(("error", f"{type(exc).__name__}: {exc}"))


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_follow_up_native_failure_retains_one_accepted_successor(
    tmp_path, backend, monkeypatch
):
    app = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    provider = SteerProvider(tmp_path / "external-children.txt")
    provider.release[0].set()
    app.runtime.adapter.provider = provider
    async with app:
        parent = await app.runtime.run("Prepare a follow-up target")
        child = await app.agents.spawn_agent(
            SpawnAgentRequest(
                request_id="host/initial",
                parent_session_id=parent.session.session_id,
                parent_run_id=parent.run.run_id,
                agent_id="general",
                task="First task",
                description="Follow-up target",
            )
        )
        await app.agents.wait_agents((child.delegation_id,))
        store = app.runtime.store
        before = store.load_delegation(parent.session.session_id, child.delegation_id)
        options = {"request_id": "host/follow-up", "mode": "follow_up"}
        select_model = app.agents._selected_model

        def select_without_locks(*args):
            def inspect():
                with store.session_mutation(before.root_session_id):
                    with store.session_mutation(child.child_session_id):
                        return store.load_session_record(child.child_session_id)

            executor = ThreadPoolExecutor(max_workers=1)
            try:
                assert (
                    executor.submit(inspect).result(timeout=10).session_id == child.child_session_id
                )
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            return select_model(*args)

        monkeypatch.setattr(app.agents, "_selected_model", select_without_locks)

        async def follow_up():
            return await app.agents.send_agent_message(
                child.delegation_id, "One follow-up input", **options
            )

        if backend == "files":
            replace = os.replace

            def fail_relation(source, destination):
                path = Path(destination)
                if path.name == "commit.json" and path.parent.name == before.root_session_id:
                    decision = json.loads(Path(source).read_text(encoding="utf-8"))
                    if decision["kind"] == "delegation" and (
                        decision["record"]["current_child_run_id"] != before.current_child_run_id
                    ):
                        raise OSError("injected follow-up binding failure")
                return replace(source, destination)

            with monkeypatch.context() as patch:
                patch.setattr(os, "replace", fail_relation)
                with pytest.raises((OSError, PersistenceError), match="injected follow-up"):
                    await follow_up()
            cold = LocalSessionStore(store.root)
        else:
            with closing(sqlite3.connect(store.database)) as connection, connection:
                connection.execute(
                    "CREATE TRIGGER fail_followup BEFORE UPDATE ON delegations "
                    "WHEN json_extract(NEW.payload, '$.current_child_run_id') "
                    "!= json_extract(OLD.payload, '$.current_child_run_id') "
                    "BEGIN SELECT RAISE(ABORT, 'injected follow-up binding failure'); END"
                )
            try:
                with pytest.raises((sqlite3.DatabaseError, PersistenceError), match="injected"):
                    await follow_up()
            finally:
                with closing(sqlite3.connect(store.database)) as connection, connection:
                    connection.execute("DROP TRIGGER fail_followup")
            cold = SQLiteSessionStore(store.database)
        cold.initialize()
        assert cold.load_delegation(before.root_session_id, before.delegation_id) == before
        assert provider.effects.read_text(encoding="utf-8").splitlines() == ["child 0"]
        reserved = cold.load_session_record(child.child_session_id)
        if backend == "files":
            assert reserved.active_run_id != before.current_child_run_id
            assert len(reserved.pending_input_ids) == 1
            accepted = cold.load_input(child.child_session_id, reserved.pending_input_ids[0])
            assert accepted.item.prospective_run_id == reserved.active_run_id

            def no_history_scan(*args, **kwargs):
                raise AssertionError("known successor recovery must use the current head")

            with monkeypatch.context() as patch:
                patch.setattr(cold, "load_runs", no_history_scan)
                assert AgentRecoveryService(cold).reconcile_root(before.root_session_id) == ()
            repaired = cold.load_delegation(before.root_session_id, before.delegation_id)
            assert repaired.current_child_run_id == reserved.active_run_id
            assert repaired.result is None
        else:
            assert reserved.active_run_id is None
            assert not reserved.pending_input_ids
            assert len(cold.load_runs(child.child_session_id)) == 1
        try:
            handle = await follow_up()
            await asyncio.wait_for(provider.entered[1].wait(), 60)
            assert handle.child_run_id != before.current_child_run_id
            if backend == "files":
                assert handle.child_run_id == reserved.active_run_id
            repeated = await follow_up()
            assert repeated.child_run_id == handle.child_run_id
            with pytest.raises(ValueError, match="already bound"):
                await app.agents.send_agent_message(child.delegation_id, "Changed input", **options)
            provider.release[1].set()
            await app.agents.wait_agents((child.delegation_id,))
            messages = store.load_run_messages(child.child_session_id, handle.child_run_id)
            inputs = [message for message in messages if message.semantic_tag == "subagent-message"]
            assert len(inputs) == 1
            fact = store.load_input(child.child_session_id, inputs[0].input_id)
            assert fact.item.prompt == "One follow-up input"
            assert fact.item.prospective_run_id == handle.child_run_id
            assert len(store.load_runs(child.child_session_id)) == 2
            assert provider.effects.read_text(encoding="utf-8").splitlines() == [
                "child 0",
                "child 1",
            ]
            await asyncio.wait_for(
                asyncio.gather(*tuple(app.agents._execution._tasks.values())), 60
            )
            await app.agents.close_agent(child.delegation_id)
            with pytest.raises(SessionBusyError, match="closed Delegation"):
                await app.agents.send_agent_message(
                    child.delegation_id,
                    "Cannot reopen",
                    request_id="host/after-close",
                    mode="follow_up",
                )
            assert (await follow_up()).control_state == "closed"
            assert len(store.load_runs(child.child_session_id)) == 2
        finally:
            for release in provider.release:
                release.set()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_independent_processes_accept_one_follow_up_and_start_it_once(tmp_path, backend):
    app = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    provider = SteerProvider(tmp_path / "external-children.txt")
    provider.release[0].set()
    app.runtime.adapter.provider = provider
    context = multiprocessing.get_context("spawn")
    start, observations = context.Event(), context.Queue()
    processes = []
    async with app:
        parent = await app.runtime.run("Prepare a shared child")
        child = await app.agents.spawn_agent(
            SpawnAgentRequest(
                request_id="host/initial",
                parent_session_id=parent.session.session_id,
                parent_run_id=parent.run.run_id,
                agent_id="general",
                task="Initial child task",
                description="Shared child",
            )
        )
        await app.agents.wait_agents((child.delegation_id,))
        try:
            for _ in range(2):
                process = context.Process(
                    target=_competing_accept,
                    args=(
                        tmp_path,
                        backend,
                        parent.session.session_id,
                        child.delegation_id,
                        start,
                        observations,
                    ),
                )
                process.start()
                processes.append(process)
            for _ in processes:
                assert await asyncio.to_thread(observations.get, timeout=30) == ("ready", None)
            start.set()
            accepted = [await asyncio.to_thread(observations.get, timeout=30) for _ in processes]
            assert all(kind == "accepted" for kind, _ in accepted), accepted
            ids = {run_id for _, value in accepted for run_id in value}
            assert len(ids) == 1
            run_id = ids.pop()
            for process in processes:
                await asyncio.to_thread(process.join, 30)
                assert process.exitcode == 0
            store = app.runtime.store
            queued = store.load_session_record(child.child_session_id)
            assert queued.active_run_id == run_id
            assert queued.pending_input_ids == ("queued_competing_followup",)
            assert len(store.load_runs(child.child_session_id)) == 2
            assert provider.effects.read_text(encoding="utf-8").splitlines() == ["child 0"]
            request = HostSessionInput(
                prompt="Concurrent follow-up",
                role="user",
                mode="follow_up",
                semantic_tag="subagent-message",
                host_source_id="test/competing-follow-up",
                queue_id="queued_competing_followup",
            )
            run = store.load_run(child.child_session_id, run_id)
            with pytest.raises(ValueError, match="already bound"):
                await app.runtime.run_prepared(
                    run, request.model_copy(update={"prompt": "Unaccepted replacement"})
                )
            handle = await app.supervisor.submit_prepared(run, request)
            await asyncio.wait_for(provider.entered[1].wait(), 60)
            provider.release[1].set()
            assert (await app.supervisor.wait(handle)).status == "completed"
            messages = store.load_run_messages(child.child_session_id, run_id)
            assert sum(message.input_id == request.queue_id for message in messages) == 1
            assert (
                store.load_input_state(child.child_session_id, request.queue_id).status
                == "consumed"
            )
            assert provider.effects.read_text(encoding="utf-8").splitlines() == [
                "child 0",
                "child 1",
            ]
        finally:
            start.set()
            for release in provider.release:
                release.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                await asyncio.to_thread(process.join, 10)
            observations.close()
            observations.join_thread()
