from __future__ import annotations

import asyncio
from threading import Event as ThreadEvent

import pytest

from ouro.application.runs.lifecycle import request_run_abort_state
from ouro.application.sessions.editing import undo_user_input
from tests.application.test_agent_call_identity import ReusedCallProvider
from tests.application.test_agent_multi_host import application
from tests.event_capture import recorded_events


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("parent_change", ["abort", "withdraw", "completed"])
async def test_child_start_rechecks_parent_after_scheduler_admission(
    tmp_path, backend, parent_change, monkeypatch
):
    app = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    other = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    provider = ReusedCallProvider(
        tmp_path / "external-children.txt",
        delegations=1,
        execution_mode="sync" if parent_change == "abort" else "async",
    )
    app.runtime.adapter.provider = provider
    entered, release = asyncio.Event(), ThreadEvent()
    loop = asyncio.get_running_loop()
    start = app.runtime.run_lifecycle.start

    def hold_child_start(**kwargs):
        if kwargs["run"].origin == "delegation":
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(30), "child start was not released"
        return start(**kwargs)

    monkeypatch.setattr(app.runtime.run_lifecycle, "start", hold_child_start)
    async with app:
        parent = await app.supervisor.submit("Recheck an admitted child's parent")
        try:
            await asyncio.wait_for(entered.wait(), 60)
            store = app.runtime.store
            relation = store.list_delegations(parent.session_id)[0]
            child_id, child_run_id = relation.child_session_id, relation.current_child_run_id
            assert store.load_run(child_id, child_run_id).status == "queued"
            if parent_change == "abort":
                # Persist from another Host without relying on its later cascade.
                await asyncio.to_thread(
                    request_run_abort_state,
                    other.runtime.store,
                    session_id=parent.session_id,
                    run_id=relation.parent_run_id,
                    reason="Cancelled while child waited for its first start",
                )
            else:
                assert (await asyncio.wait_for(app.supervisor.wait(parent), 60)).status == (
                    "completed"
                )
                if parent_change == "withdraw":
                    await asyncio.to_thread(undo_user_input, other.runtime.store, parent.session_id)
            provider.release[0].set()
            release.set()
            result = await asyncio.wait_for(app.supervisor.wait_run(child_id, child_run_id), 60)
            events = recorded_events(child_id)
            if parent_change == "completed":
                assert result.status == "completed"
                assert provider.effects.read_text(encoding="utf-8").splitlines() == ["child 0"]
                assert any(event.event_type == "run_started" for event in events)
            else:
                assert result.status == "aborted"
                assert not provider.effects.exists()
                assert provider.children == 0
                assert not store.load_run_messages(child_id, child_run_id)
                assert not any(event.event_type == "run_started" for event in events)
            await app.agents._finalize_result(relation.delegation_id)
            saved = store.load_delegation(parent.session_id, relation.delegation_id)
            assert saved.result.child_run_id == child_run_id
            assert saved.result.terminal_status == result.status
        finally:
            release.set()
            for child_release in provider.release:
                child_release.set()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_child_start_commit_serializes_with_parent_cancellation(
    tmp_path, backend, monkeypatch
):
    app = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    other = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    provider = ReusedCallProvider(tmp_path / "external-children.txt", delegations=1)
    app.runtime.adapter.provider = provider
    entered, attempted, release = asyncio.Event(), asyncio.Event(), ThreadEvent()
    loop = asyncio.get_running_loop()
    order = []
    starting = []
    commit = app.runtime.store.commit_session
    mutation = other.runtime.store.session_mutation

    def hold_start_commit(request):
        is_start = any(
            run.origin == "delegation" and run.status == "running" for run in request.runs
        ) and any(event.event_type == "run_started" for event in request.events)
        if is_start:
            starting.extend(request.runs)
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(30), "child start commit was not released"
        result = commit(request)
        if is_start:
            order.append("child_start")
        return result

    def observe_abort_mutation(session_id):
        loop.call_soon_threadsafe(attempted.set)
        return mutation(session_id)

    def request_abort(parent, child_run):
        result = request_run_abort_state(
            other.runtime.store,
            session_id=parent.session_id,
            run_id=child_run.parent_run_id,
            reason="Cancellation raced the child start commit",
        )
        order.append("parent_abort")
        return result

    monkeypatch.setattr(app.runtime.store, "commit_session", hold_start_commit)
    monkeypatch.setattr(other.runtime.store, "session_mutation", observe_abort_mutation)
    async with app:
        parent = await app.supervisor.submit("Serialize child start with parent abort")
        abort = None
        try:
            await asyncio.wait_for(entered.wait(), 60)
            child_run = starting[0]
            abort = asyncio.create_task(asyncio.to_thread(request_abort, parent, child_run))
            await asyncio.wait_for(attempted.wait(), 60)
            release.set()
            await asyncio.wait_for(abort, 60)
            assert order == ["child_start", "parent_abort"]
            provider.release[0].set()
            await app.runtime.abort_run(
                session_id=parent.session_id, run_id=child_run.parent_run_id
            )
            assert (
                await asyncio.wait_for(
                    app.supervisor.wait_run(child_run.session_id, child_run.run_id),
                    60,
                )
            ).status in {"completed", "aborted"}
        finally:
            release.set()
            for child_release in provider.release:
                child_release.set()
            if abort is not None:
                await asyncio.gather(abort, return_exceptions=True)
