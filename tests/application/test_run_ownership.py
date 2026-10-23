from __future__ import annotations

import asyncio
from threading import Event, get_ident

import pytest

from ouro.application.run_ownership import RunExecutionOwnership
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("status", ["waiting_approval", "waiting_user", "waiting_child"])
async def test_parked_run_handoff_waits_for_another_host_to_release(tmp_path, backend, status):
    def store():
        return (
            LocalSessionStore(tmp_path)
            if backend == "files"
            else SQLiteSessionStore(tmp_path / "sessions.sqlite3")
        )

    first_store, second_store = store(), store()
    first_store.initialize()
    second_store.initialize()
    session = SessionRecord()
    run = RunRecord(session_id=session.session_id, status=status)
    first_store.create_session(session)
    first_store.save_run(run)
    first, second = RunExecutionOwnership(first_store), RunExecutionOwnership(second_store)
    lock = await first.acquire(session.session_id, run.run_id)
    pending = asyncio.create_task(
        second.acquire_parked(
            session.session_id,
            run.run_id,
            expected_status=status,
        )
    )
    try:
        await asyncio.sleep(0.05)
        assert not pending.done()
    finally:
        await first.release(run.run_id, lock)
    acquired = await asyncio.wait_for(pending, 2)
    assert acquired is not None
    await second.release(run.run_id, acquired)


@pytest.mark.parametrize("outcome", ["timeout", "resumed", "cancelled"])
async def test_parked_handoff_never_steals_an_existing_owner(tmp_path, outcome):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord()
    run = RunRecord(session_id=session.session_id, status="waiting_approval")
    store.create_session(session)
    store.save_run(run)
    first, second = RunExecutionOwnership(store), RunExecutionOwnership(store)
    lock = await first.acquire(session.session_id, run.run_id)
    pending = asyncio.create_task(
        second.acquire_parked(
            session.session_id,
            run.run_id,
            expected_status="waiting_approval",
            timeout=0.1,
        )
    )
    try:
        await asyncio.sleep(0.01)
        if outcome == "resumed":
            store.save_run(run.model_copy(update={"status": "running"}))
        if outcome == "cancelled":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            assert await asyncio.wait_for(pending, 1) is None
        assert store.is_session_locked(session.session_id)
    finally:
        await first.release(run.run_id, lock)


@pytest.mark.asyncio
async def test_run_execution_ownership_tracks_lock_and_signal_lifecycle(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(session_id="ses_owner")
    run = RunRecord(run_id="run_owner", session_id=session.session_id, status="running")
    store.create_session(session)
    store.save_run(run)
    ownership = RunExecutionOwnership(store)

    lock = await ownership.acquire(session.session_id, run.run_id)
    signal = await ownership.activate_signal(session.session_id, run.run_id)

    assert lock is not None
    assert ownership.is_locked(session.session_id)
    assert ownership.signal_for(run.run_id) is signal
    assert not signal.is_aborted()

    store.save_run(run.model_copy(update={"abort_requested": True}))
    settlement = asyncio.create_task(
        RunExecutionOwnership(store).wait_for_abort_settlement(
            session.session_id,
            run.run_id,
            poll_interval=0.001,
        )
    )
    await asyncio.sleep(0)
    assert not settlement.done()
    await ownership.release(run.run_id, lock)
    await asyncio.wait_for(settlement, timeout=1)

    # The poller captures the cancellation request once. Its signal does not
    # promise terminal settlement or keep refreshing after cancellation. The
    # runtime finalizes the Run only after the execution owner has settled.
    async with asyncio.timeout(30):
        await signal.wait()
    assert signal.is_aborted()
    result = ownership.aborted_result(
        session_id=session.session_id,
        run_id=run.run_id,
        final_text="partial",
        emitted=[],
        tool_results=[],
    )
    assert result is not None
    assert result.run.abort_requested is True
    assert result.run.status == "running"

    await ownership.release_signal(run.run_id, signal)
    assert ownership.signal_for(run.run_id) is None
    assert not ownership.is_locked(session.session_id)


async def test_cancelled_acquisition_releases_lock_acquired_by_owned_thread(tmp_path, monkeypatch):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Cancelled lock acquisition")
    store.create_session(session)
    ownership = RunExecutionOwnership(store)
    entered, release = Event(), Event()
    acquire = store.acquire_session_lock

    def paused_acquire(*args):
        lock = acquire(*args)
        assert lock is not None
        entered.set()
        assert release.wait(5)
        return lock

    monkeypatch.setattr(store, "acquire_session_lock", paused_acquire)
    pending = asyncio.create_task(ownership.acquire(session.session_id, "run_cancelled"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not store.is_session_locked(session.session_id)


@pytest.mark.asyncio
async def test_abort_signal_checks_never_read_durable_state_on_the_event_loop(
    tmp_path,
    monkeypatch,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(session_id="ses_abort_fast_path")
    run = RunRecord(run_id="run_abort_fast_path", session_id=session.session_id, status="running")
    store.create_session(session)
    store.save_run(run)
    ownership = RunExecutionOwnership(store)
    loop_thread = get_ident()
    load_threads: list[int] = []
    original_load_run = store.load_run

    def recording_load_run(session_id: str, run_id: str) -> RunRecord:
        load_threads.append(get_ident())
        return original_load_run(session_id, run_id)

    monkeypatch.setattr(store, "load_run", recording_load_run)
    signal = await ownership.activate_signal(session.session_id, run.run_id)

    for _ in range(100):
        assert not signal.is_aborted()
    for _ in range(100):
        if load_threads:
            break
        await asyncio.sleep(0.01)

    assert load_threads
    assert loop_thread not in load_threads
    await ownership.release_signal(run.run_id, signal)
