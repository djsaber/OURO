from __future__ import annotations

import asyncio

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

    lock = ownership.acquire(session.session_id, run.run_id)
    signal = ownership.activate_signal(session.session_id, run.run_id)

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
    ownership.release(lock)
    await asyncio.wait_for(settlement, timeout=1)

    aborted = run.model_copy(update={"status": "aborted", "abort_requested": True})
    store.save_run(aborted)
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

    ownership.release_signal(run.run_id, signal)
    assert ownership.signal_for(run.run_id) is None
    assert not ownership.is_locked(session.session_id)
