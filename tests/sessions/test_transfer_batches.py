from __future__ import annotations

import os
import sqlite3

import pytest

import ouro.sessions.backends.files.store as file_store
from ouro.core.errors import PersistenceError
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
        MessageRecord.text(
            session_id=session.session_id, role="user", text="历史\nmessage", sequence=index + 1
        )
        for index in range(100)
    ]
    session = session.model_copy(
        update={
            "message_count": len(messages),
            "last_message_id": messages[-1].message_id,
            "run_count": len(runs),
        }
    )
    return SessionBundle(
        sessions=[
            PortableSession.from_snapshot(
                snapshot=SessionSnapshot(
                    session=session,
                    messages=messages,
                    runs=runs,
                    harness_versions=[
                        HarnessVersion(
                            session_id=session.session_id, version_id=session.harness_version
                        )
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
    assert store.load_session("ses_transfer") == bundle.sessions[0].snapshot.materialize()
    # Canonical journals synchronize batches; the native location index commits
    # one transaction. Neither boundary synchronizes each individual message.
    assert 0 < calls < 30


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_imported_facts_remain_queryable_after_reopen_and_append(tmp_path, backend):
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
    reopened = open_store()
    assert reopened.load_session("ses_transfer") == bundle.sessions[0].snapshot.materialize()
    reopened.append_message(MessageRecord.text(session_id="ses_transfer", role="user", text="next"))
    page, more = open_store().load_messages_after("ses_transfer", after_sequence=100, limit=1)
    assert len(page) == 1 and page[0].text_content == "next" and not more
    assert "events" not in reopened.load_session("ses_transfer").model_dump()


def test_file_import_native_failure_does_not_publish_sessions(tmp_path, monkeypatch):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    write = file_store.write_session

    def fail(connection, run, **kwargs):
        write(connection, run, **kwargs)
        raise sqlite3.OperationalError("injected native failure")

    monkeypatch.setattr(file_store, "write_session", fail)
    with pytest.raises(PersistenceError, match="native failure"):
        store.import_bundle(history_bundle())
    assert store.list_sessions() == []
    assert not list(tmp_path.glob(".session-import-*"))
