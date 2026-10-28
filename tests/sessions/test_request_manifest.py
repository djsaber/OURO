from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

import pytest

from ouro.core.context import ContextBuilder
from ouro.core.context_snapshots import ModelRequestHeader, RequestManifestRecord
from ouro.core.errors import PersistenceError
from ouro.core.messages import MessageRecord
from ouro.core.session_commits import SessionCommit
from ouro.core.state import SessionRecord
from ouro.local_locks import FileMutationLock
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


@pytest.fixture(params=[LocalSessionStore, SQLiteSessionStore])
def store(tmp_path, request):
    result = request.param(
        tmp_path / "sessions.db" if request.param is SQLiteSessionStore else tmp_path / "files"
    )
    result.initialize()
    return result


def publish(store, session, source):
    view = ContextBuilder().build(messages=[source], source_messages=[source], tools=[])
    header = ModelRequestHeader(
        session_id=session.session_id,
        run_id="run",
        turn_id="turn",
        model_round=1,
        provider_id="test",
        model_id="test",
        metrics=view.metrics,
    )
    store.commit_session(
        SessionCommit(
            before=store.load_session_record(session.session_id),
            request_header=header,
            request_manifest=RequestManifestRecord(
                context_id=header.context_id, manifest=view.manifest
            ),
        )
    )
    return header, view


@pytest.mark.parametrize("size", [64, 1024 * 1024])
def test_manifest_does_not_copy_canonical_body_and_query_reads_only_its_source(
    store, monkeypatch, size
):
    session = SessionRecord()
    store.create_session(session)
    for _ in range(4):
        store.append_message(
            MessageRecord.text(session_id=session.session_id, role="user", text="unrelated")
        )
    source = MessageRecord.text(session_id=session.session_id, role="user", text="x" * size)
    store.append_message(source)
    header, view = publish(store, session, source)
    if isinstance(store, LocalSessionStore):
        payload = (
            store.sessions_dir
            / session.session_id
            / "request-content"
            / f"{header.context_id}.json"
        ).read_text(encoding="utf-8")
    else:
        with closing(sqlite3.connect(store.database)) as connection:
            payload = connection.execute("SELECT payload FROM latest_contexts").fetchone()[0]
    assert "x" * 64 not in payload
    assert len(payload) < 1500
    assert '"provider_context"' not in payload
    reads = []
    original = store.load_message

    def load(session_id, message_id):
        assert message_id == source.message_id
        reads.append(message_id)
        return original(session_id, message_id)

    monkeypatch.setattr(store, "load_message", load)
    assert store.load_latest_context(session.session_id).provider_context == view.provider_context
    assert reads == [source.message_id]


def test_query_keeps_source_alive_through_concurrent_withdrawal(store, monkeypatch):
    session = SessionRecord()
    store.create_session(session)
    source = MessageRecord.text(session_id=session.session_id, role="user", text="actual input")
    store.append_message(source)
    _, view = publish(store, session, source)
    entered, release = Event(), Event()
    original = store.load_message

    def paused(session_id, message_id):
        entered.set()
        assert release.wait(10)
        return original(session_id, message_id)

    def withdraw():
        store.truncate_messages(
            session.session_id,
            before_message_id=source.message_id,
            expected_last_message_id=source.message_id,
        )

    monkeypatch.setattr(store, "load_message", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(store.load_latest_context, session.session_id)
        try:
            assert entered.wait(10)
            if isinstance(store, LocalSessionStore):
                lock = FileMutationLock(store._mutation_lock_path(session.session_id))
                try:
                    assert not lock.acquire(timeout=0)
                finally:
                    lock.release()
            else:
                withdraw()
        finally:
            release.set()
        retained = pending.result(timeout=10)
    if isinstance(store, LocalSessionStore):
        withdraw()
    assert retained.provider_context == view.provider_context
    assert store.load_latest_context(session.session_id) is None


def test_invalid_manifest_reference_is_not_silently_reprojected(store):
    session = SessionRecord()
    store.create_session(session)
    source = MessageRecord.text(session_id=session.session_id, role="user", text="missing")
    publish(store, session, source)
    with pytest.raises(PersistenceError, match="message not found"):
        store.load_latest_context(session.session_id)
