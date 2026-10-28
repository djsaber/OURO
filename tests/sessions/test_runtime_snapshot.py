from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ouro.core.compaction import CompactionRecord
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from tests.session_summaries import commit_summary


@pytest.fixture(params=[LocalSessionStore, SQLiteSessionStore])
def store(tmp_path, request):
    result = request.param(
        tmp_path / "sessions.db" if request.param is SQLiteSessionStore else tmp_path / "files"
    )
    result.initialize()
    return result


@pytest.mark.parametrize("history_size", [4, 2048])
def test_runtime_snapshot_does_not_read_message_or_summary_storage(
    store, monkeypatch, history_size
):
    session = SessionRecord()
    run = RunRecord(session_id=session.session_id, status="completed")
    session.last_run_id = run.run_id
    messages = [
        MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            role="user",
            text=f"{index}:" + "x" * 128,
        )
        for index in range(history_size)
    ]
    store.create_session_snapshot(session, messages)
    store.save_run(run)
    summary = CompactionRecord(
        session_id=session.session_id,
        strategy_id="test",
        trigger="manual",
        summary="covered history",
        covered_message_count=history_size - 1,
        covered_end_message_id=messages[-2].message_id,
        history_boundary_message_id=messages[-1].message_id,
    )
    commit_summary(store, summary)

    if isinstance(store, LocalSessionStore):
        original = Path.open

        def guarded(path, *args, **kwargs):
            if path.name == "messages.jsonl":
                pytest.fail(f"execution metadata read history: {path}")
            return original(path, *args, **kwargs)

        connect = sqlite3.connect

        def authorize(action, table, column, database, source):
            if action == sqlite3.SQLITE_READ and table in {"locations", "compactions"}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        def guarded_connection(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_authorizer(authorize)
            return connection

        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", guarded)
            patch.setattr(sqlite3, "connect", guarded_connection)
            snapshot = store.load_runtime_session(session.session_id)
    else:
        denied = []

        def authorize(action, table, column, database, source):
            if action == sqlite3.SQLITE_READ and table in {"messages", "compactions"}:
                denied.append((table, column))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        with store.session_read(session.session_id) as connection:
            connection.set_authorizer(authorize)
            try:
                snapshot = store.load_runtime_session(session.session_id)
            finally:
                connection.set_authorizer(None)
        assert denied == []

    assert snapshot.session.message_count == history_size
    assert snapshot.session.context_selection.compaction_id == summary.compaction_id
    assert snapshot.runs == [run]
    # Full history is an explicit, separate read contract for inspection/transfer.
    full = store.load_session(session.session_id)
    observed = store.load_session_observation(session.session_id)
    assert len(full.messages) == len(observed.messages) == history_size
    assert full.active_compaction == observed.active_compaction == summary


def test_run_membership_uses_metadata_and_tracks_withdrawal(store, monkeypatch):
    session = SessionRecord()
    first = MessageRecord.text(session_id=session.session_id, run_id="run_a", role="user", text="a")
    last = MessageRecord.text(
        session_id=session.session_id, run_id="run_b", role="assistant", text="b"
    )
    store.create_session(session)
    store.append_messages([first, last])

    def forbid_body(*args, **kwargs):
        pytest.fail("membership must not materialize message bodies")

    with monkeypatch.context() as patch:
        if isinstance(store, LocalSessionStore):
            patch.setattr(store._messages, "fact", forbid_body)
        else:
            patch.setattr(store, "_materialize_message", forbid_body)
        assert store.has_run_messages(session.session_id, "run_a")
        assert store.has_run_messages(session.session_id, "run_b")
        assert not store.has_run_messages(session.session_id, "run_missing")
        assert store.has_input_messages(session.session_id)
    store.truncate_messages(
        session.session_id,
        before_message_id=last.message_id,
        expected_last_message_id=last.message_id,
    )
    assert store.has_run_messages(session.session_id, "run_a")
    assert not store.has_run_messages(session.session_id, "run_b")
    store.truncate_messages(
        session.session_id,
        before_message_id=first.message_id,
        expected_last_message_id=first.message_id,
    )
    assert not store.has_input_messages(session.session_id)
