from __future__ import annotations

import asyncio
from threading import Event, get_ident

import pytest

from ouro.application.run_ownership import RunExecutionOwnership
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore


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

    settlement = asyncio.create_task(
        RunExecutionOwnership(store).wait_for_settlement(
            session.session_id,
            run.run_id,
            poll_interval=0.001,
        )
    )
    await asyncio.sleep(0)
    assert not settlement.done()
    await ownership.release(run.run_id, lock)
    await asyncio.wait_for(settlement, timeout=1)

    aborted = run.model_copy(update={"status": "aborted", "abort_requested": True})
    store.save_run(aborted)
    for _ in range(100):
        if signal.is_aborted():
            break
        await asyncio.sleep(0.01)
    assert signal.is_aborted()
    result = ownership.aborted_result(
        session_id=session.session_id,
        run_id=run.run_id,
        final_text="partial",
        emitted=[],
        tool_results=[],
    )
    assert result is not None
    assert result.run.status == "aborted"

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
    assert not ownership._release_events


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
