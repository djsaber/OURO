from __future__ import annotations

import os

import pytest

from ouro.core.errors import PersistenceError
from ouro.core.events import EventRecord
from ouro.core.harness import HarnessVersion
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions.backends.files.store import LocalSessionStore
from ouro.sessions.backends.sqlite.store import SQLiteSessionStore
from ouro.sessions.base import SessionSnapshot
from ouro.sessions.portable import PortableSession, SessionBundle


def history_bundle() -> SessionBundle:
    session = SessionRecord(session_id="ses_transfer")
    runs = [RunRecord(session_id=session.session_id, status="completed") for _ in range(2)]
    messages = [
        MessageRecord.text(session_id=session.session_id, role="user", text="历史\nmessage")
        for _ in range(100)
    ]
    session = session.model_copy(
        update={"message_count": len(messages), "last_message_id": messages[-1].message_id}
    )
    return SessionBundle(
        sessions=[
            PortableSession(
                snapshot=SessionSnapshot(
                    session=session,
                    messages=messages,
                    runs=runs,
                    harness_versions=[
                        HarnessVersion(
                            session_id=session.session_id, version_id=session.harness_version
                        )
                    ],
                    events=[
                        EventRecord(
                            session_id=session.session_id,
                            run_id=runs[index % 2].run_id,
                            sequence=index + 1,
                            event_type="model_text_delta",
                            payload={"delta": "你好\n\u2028\u2029"},
                        )
                        for index in range(600)
                    ],
                )
            )
        ]
    )


def test_file_import_syncs_journals_in_batches(tmp_path, monkeypatch):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    sync = os.fsync
    calls = 0

    def counted_sync(fd):
        nonlocal calls
        calls += 1
        return sync(fd)

    monkeypatch.setattr(os, "fsync", counted_sync)
    bundle = history_bundle()
    store.import_bundle(bundle)
    assert store.load_session("ses_transfer") == bundle.sessions[0].snapshot
    # Durability belongs to completed files, not each of the 700 journal records.
    assert 0 < calls < 30


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_imported_events_remain_queryable_after_reopen_and_append(tmp_path, backend):
    def open_store():
        return (
            LocalSessionStore(tmp_path)
            if backend == "files"
            else SQLiteSessionStore(tmp_path / "sessions.db")
        )

    store = open_store()
    store.initialize()
    bundle = history_bundle()
    store.import_bundle(bundle)
    store = open_store()
    snapshot = bundle.sessions[0].snapshot
    assert store.load_session("ses_transfer") == snapshot
    for run in snapshot.runs:
        events, cursor = store.load_events_page("ses_transfer", run_id=run.run_id, limit=1000)
        assert events == [event for event in snapshot.events if event.run_id == run.run_id]
        assert cursor is None
    events, more = store.load_events_after("ses_transfer", after_sequence=510, limit=100)
    assert events == snapshot.events[510:]
    assert not more
    appended = store.append_ordered_event(
        EventRecord(
            session_id="ses_transfer", run_id=snapshot.runs[1].run_id, event_type="run_completed"
        )
    )
    assert appended.sequence == 601
    assert open_store().load_session("ses_transfer").events == [*snapshot.events, appended]

    # A second batch must preserve existing indexes and index new Runs at EOF.
    run = RunRecord(session_id="ses_transfer", status="completed")
    store.save_run(run)
    batch = [
        EventRecord(
            session_id="ses_transfer",
            run_id=run.run_id,
            sequence=sequence,
            event_type="model_text_delta",
            payload={"delta": "再见"},
        )
        for sequence in range(602, 901)
    ]
    store.append_events(batch)
    events, cursor = open_store().load_events_page("ses_transfer", run_id=run.run_id, limit=1000)
    assert events == batch
    assert cursor is None
    events, more = open_store().load_events_after("ses_transfer", after_sequence=800, limit=1000)
    assert events == batch[199:]
    assert not more


def test_file_import_sync_failure_does_not_publish_sessions(tmp_path, monkeypatch):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    sync = os.fsync

    def fail_event_sync(fd):
        for journal in tmp_path.glob(".session-import-*/sessions/*/events.jsonl"):
            if journal.stat().st_ino == os.fstat(fd).st_ino:
                raise OSError("injected event sync failure")
        return sync(fd)

    monkeypatch.setattr(os, "fsync", fail_event_sync)
    with pytest.raises(PersistenceError, match="event batch"):
        store.import_bundle(history_bundle())
    assert store.list_sessions() == []
    assert not list(tmp_path.glob(".session-import-*"))


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_event_batch_rejects_foreign_session_before_writing(tmp_path, backend):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    with pytest.raises(PersistenceError, match="one Session"):
        store.append_events(
            [
                EventRecord(session_id=session.session_id, event_type="run_completed"),
                EventRecord(session_id="ses_foreign", event_type="run_completed"),
            ]
        )
    assert store.load_session(session.session_id).events == []


def test_sqlite_event_batch_failure_rolls_back_inserted_prefix(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    event = EventRecord(session_id=session.session_id, event_type="run_completed")
    with pytest.raises(PersistenceError, match="event batch"):
        store.append_events([event, event])
    assert store.load_session(session.session_id).events == []
