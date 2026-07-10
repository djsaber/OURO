from typing import Any

import pytest

from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore


def test_local_file_store_round_trip(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Local test", current_model="faux")
    run = RunRecord(session_id=session.session_id, status="completed", model="faux")
    message = MessageRecord.text(
        session_id=session.session_id,
        run_id=run.run_id,
        role="user",
        text="hello",
        sequence=1,
    )
    event = EventRecord(
        session_id=session.session_id,
        run_id=run.run_id,
        event_type="run_started",
        sequence=1,
    )

    store.create_session(session)
    store.save_run(run)
    store.append_message(message)
    store.append_event(event)

    snapshot = store.load_session(session.session_id)

    assert snapshot.session.title == "Local test"
    assert snapshot.messages[0].text_content == "hello"
    assert snapshot.events[0].event_type == "run_started"
    assert snapshot.runs[0].status == "completed"
    assert store.list_sessions()[0].session_id == session.session_id


def test_local_file_store_rebuilds_corrupt_index(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Indexed")
    store.create_session(session)
    (tmp_path / "sessions" / "index.json").write_text("{not-json", encoding="utf-8")

    repaired = LocalSessionStore(tmp_path)
    repaired.initialize()
    index: dict[str, Any] = repaired.load_index()

    assert index["sessions"][0]["session_id"] == session.session_id


def test_local_file_store_recovers_interrupted_run(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Interrupted", status="running")
    run = RunRecord(session_id=session.session_id, status="running", model="faux")
    store.create_session(session)
    store.save_run(run)

    recovered = LocalSessionStore(tmp_path).load_session(session.session_id)

    assert recovered.session.status == "idle"
    assert recovered.session.metadata["recovered_incomplete_runs"] is True
    assert recovered.runs[0].status == "recoverable"
    assert recovered.runs[0].metadata["recovered_from_status"] == "running"
    assert recovered.events[-1].event_type == "run_status_changed"
    assert recovered.events[-1].payload["to_status"] == "recoverable"

    reloaded = LocalSessionStore(tmp_path).load_session(session.session_id)
    recovery_events = [
        event for event in reloaded.events if event.payload.get("to_status") == "recoverable"
    ]
    assert len(recovery_events) == 1


def test_local_file_store_lock_prevents_active_run_recovery(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Locked", status="running")
    run = RunRecord(session_id=session.session_id, status="running", model="faux")
    store.create_session(session)
    store.save_run(run)
    lock = store.acquire_session_lock(session.session_id, run.run_id)

    try:
        snapshot = store.load_session(session.session_id)
    finally:
        assert lock is not None
        lock.release()

    assert snapshot.session.status == "running"
    assert snapshot.runs[0].status == "running"
    assert not any(event.event_type == "run_status_changed" for event in snapshot.events)
    assert not store.is_session_locked(session.session_id)


def test_released_lock_file_does_not_block_interrupted_run_recovery(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Interrupted", status="running")
    run = RunRecord(session_id=session.session_id, status="running", model="faux")
    store.create_session(session)
    store.save_run(run)
    lock = store.acquire_session_lock(session.session_id, run.run_id)
    assert lock is not None
    lock.release()

    recovered = LocalSessionStore(tmp_path).load_session(session.session_id)

    assert recovered.runs[0].status == "recoverable"


def test_store_rejects_path_like_record_ids(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()

    with pytest.raises(ValueError, match="invalid session id"):
        store.load_session("../outside")

    session = SessionRecord(title="Safe")
    store.create_session(session)
    unsafe_run = RunRecord(session_id=session.session_id, run_id="../outside")
    with pytest.raises(ValueError, match="invalid run id"):
        store.save_run(unsafe_run)
