import sqlite3

import pytest

from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import SQLiteSessionStore


@pytest.mark.parametrize("nested", [False, True])
def test_sqlite_current_draft_is_visible_and_never_downgrades_an_outer_commit(
    tmp_path, monkeypatch, nested
):
    from contextlib import nullcontext

    from ouro.core.response_drafts import ResponseDraftChunk
    from tests.response_drafts import persist_response_draft

    store = SQLiteSessionStore(tmp_path / "ouro.db")
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(session_id=session.session_id, status="running")
    store.save_run(run)
    header = persist_response_draft(store, run, turn_id="turn")
    modes = []
    transaction = store._transaction_connection

    def capture():
        connection = transaction()
        modes.append(connection.execute("PRAGMA synchronous").fetchone()[0])
        return connection

    monkeypatch.setattr(store, "_transaction_connection", capture)
    owner = store.acquire_session_lock(session.session_id, run.run_id)
    assert owner is not None
    with owner:
        with store.session_mutation(session.session_id) if nested else nullcontext():
            store.append_response_draft(
                session.session_id,
                header.context_id,
                ResponseDraftChunk(channel="text", content="visible"),
            )
        assert modes and set(modes) == {2 if nested else 1}
        reader = SQLiteSessionStore(store.database)
        assert (
            reader.load_response_draft(session.session_id, header.context_id).chunks[0].content
            == "visible"
        )

        modes.clear()
        store.save_run(run)
        assert modes and set(modes) == {2}


def test_sqlite_fact_suffix_queries_use_session_scoped_indexes(tmp_path) -> None:
    store = SQLiteSessionStore(tmp_path / "ouro.db")
    store.initialize()

    connection = sqlite3.connect(store.database)
    try:
        observation_plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT payload FROM runs "
            "WHERE session_id = ? AND modified_revision > ? "
            "ORDER BY modified_revision, run_id LIMIT 128",
            ("session_1", 100_000),
        ).fetchall()
        message_plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT payload FROM messages "
            "WHERE session_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
            ("session_1", 100_000, 101),
        ).fetchall()
    finally:
        connection.close()

    assert any("USING INDEX runs_modified" in str(row[3]) for row in observation_plan)
    assert any(
        "USING INDEX" in str(row[3]) and "session_id=? AND sequence>?" in str(row[3])
        for row in message_plan
    )


def test_sqlite_fact_position_shares_snapshot_boundary(tmp_path, monkeypatch):
    from ouro.application.sessions.service import SessionService

    store = SQLiteSessionStore(tmp_path / "ouro.db")
    store.initialize()
    session = SessionRecord()
    store.create_session(session)
    store.append_message(
        MessageRecord.text(session_id=session.session_id, role="user", text="first")
    )
    snapshot = SessionService(store).observation_snapshot(session.session_id)
    store.append_message(
        MessageRecord.text(session_id=session.session_id, role="user", text="second")
    )
    batch, more = SessionService(store).observation_changes(
        session.session_id, after=snapshot.position, history_epoch=0, limit=10
    )
    assert snapshot.position.message_sequence == 1
    assert not more
    assert [message.text_content for message in batch.messages] == ["second"]
    assert batch.position.message_sequence == 2
