from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from ouro.core.compaction import CompactionRecord
from ouro.core.errors import PersistenceError, SessionRecoveryRequiredError
from ouro.core.harness import HarnessVersion
from ouro.core.messages import MessageRecord
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


@pytest.fixture(params=[LocalSessionStore, SQLiteSessionStore])
def store(tmp_path, request):
    result = request.param(
        tmp_path / "sessions.sqlite3" if request.param is SQLiteSessionStore else tmp_path / "files"
    )
    result.initialize()
    return result


def seed(store, count, *, affected):
    session = SessionRecord()
    checkpoint = HarnessVersion(session_id=session.session_id)
    session.harness_version = checkpoint.version_id
    messages = [
        MessageRecord.text(session_id=session.session_id, role="user", text=f"message {i}")
        for i in range(12)
    ]
    for message in messages:
        message.before_harness_version = checkpoint.version_id
    records = []
    for index in range(count + (3 if affected else 0)):
        covered = 10 if index == count + 1 else 2
        record = CompactionRecord(
            session_id=session.session_id,
            previous_compaction_id=records[-1].compaction_id if records else None,
            strategy_id="test",
            trigger="manual",
            summary=f"Summary {index}: " + "x" * 65_536,
            covered_message_count=covered,
            covered_end_message_id=messages[covered - 1].message_id,
            history_boundary_message_id=messages[-1 if index >= count else 2].message_id,
        )
        records.append(record)
    session.context_selection.compaction_id = records[-1].compaction_id
    store.create_session_snapshot(
        session, messages, compactions=records, harness_versions=[checkpoint]
    )
    return store.load_session_record(session.session_id), messages, records


@pytest.mark.parametrize("count", [4, 128])
@pytest.mark.parametrize("affected", [False, True])
def test_withdrawal_reads_and_writes_only_changed_summary_bodies(
    store, count, affected, monkeypatch
):
    session, messages, records = seed(store, count, affected=affected)
    reads, writes = [], []
    moving = records[count] if affected else None
    database = (
        store.sessions_dir / session.session_id / "session-data.sqlite3"
        if isinstance(store, LocalSessionStore)
        else store.database
    )
    connect = sqlite3.connect

    def trace(statement):
        if statement.startswith("SELECT payload FROM compactions"):
            reads.append(statement)
        if statement.startswith("UPDATE compactions SET"):
            writes.append(statement)

    def traced_connection(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connection)
    with closing(connect(database)) as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT compaction_id, previous_compaction_id, "
            "covered_end_message_id, history_boundary_message_id FROM compactions "
            "WHERE session_id = ? ORDER BY row_id",
            (session.session_id,),
        ).fetchall()
        assert "COVERING INDEX compaction_references" in str(plan)
    store.truncate_messages(
        session.session_id,
        before_message_id=messages[8].message_id,
        expected_last_message_id=messages[-1].message_id,
    )
    assert len(reads) == len(writes) == int(affected)
    if moving is not None:
        assert all(moving.compaction_id in value for value in (*reads, *writes))
    monkeypatch.undo()
    retained = records[:count]
    if moving is not None:
        retained.append(
            moving.model_copy(update={"history_boundary_message_id": messages[7].message_id})
        )
    assert store.load_compactions(session.session_id) == retained
    assert store.load_active_compaction(session.session_id) == retained[-1]


def test_withdrawal_summary_changes_recover_or_roll_back_natively(store, monkeypatch):
    session, messages, records = seed(store, 4, affected=True)
    moving = records[4]
    database = (
        store.sessions_dir / session.session_id / "session-data.sqlite3"
        if isinstance(store, LocalSessionStore)
        else store.database
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER fail_boundary BEFORE UPDATE ON compactions "
            f"WHEN OLD.compaction_id = '{moving.compaction_id}' "
            "BEGIN SELECT RAISE(ABORT, 'injected summary boundary failure'); END"
        )
    with pytest.raises(PersistenceError, match="injected summary boundary failure"):
        store.truncate_messages(
            session.session_id,
            before_message_id=messages[8].message_id,
            expected_last_message_id=messages[-1].message_id,
        )
    monkeypatch.undo()
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TRIGGER fail_boundary")
    if isinstance(store, LocalSessionStore):
        reopened = LocalSessionStore(store.sessions_dir.parent)
        with pytest.raises(SessionRecoveryRequiredError):
            reopened.load_compactions(session.session_id)
        with reopened.session_mutation(session.session_id):
            pass
        store = reopened
    else:
        assert store.load_session_record(session.session_id) == session
        assert store.load_compactions(session.session_id) == records
        store.truncate_messages(
            session.session_id,
            before_message_id=messages[8].message_id,
            expected_last_message_id=messages[-1].message_id,
        )
    active = store.load_active_compaction(session.session_id)
    assert active == moving.model_copy(
        update={"history_boundary_message_id": messages[7].message_id}
    )
    assert store.load_compactions(session.session_id) == [*records[:4], active]
    assert store.load_session_record(session.session_id).message_count == 8
