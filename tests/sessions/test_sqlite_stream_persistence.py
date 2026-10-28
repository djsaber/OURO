import sqlite3

import pytest

import ouro.sessions.backends.sqlite.store as sqlite_store
from ouro.core.events import EventRecord
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


def test_sqlite_observation_and_replay_queries_use_session_scoped_indexes(tmp_path) -> None:
    store = SQLiteSessionStore(tmp_path / "ouro.db")
    store.initialize()

    connection = sqlite3.connect(store.database)
    try:
        observation_plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT payload FROM events "
            "WHERE session_id = ? AND run_id = ? ORDER BY row_id",
            ("session_1", "run_1"),
        ).fetchall()
        replay_plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT payload FROM events "
            "WHERE session_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
            ("session_1", 100_000, 101),
        ).fetchall()
    finally:
        connection.close()

    assert any("USING INDEX events_session_run_order" in str(row[3]) for row in observation_plan)
    assert any(
        "USING INDEX" in str(row[3]) and "session_id=? AND sequence>?" in str(row[3])
        for row in replay_plan
    )


def test_sqlite_observation_high_water_shares_the_snapshot_read_boundary(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteSessionStore(tmp_path / "ouro.db")
    store.initialize()
    session = SessionRecord(title="Atomic observation")
    store.create_session(session)
    run = RunRecord(run_id="run_active", session_id=session.session_id, status="running")
    store.save_run(run)
    store.save_session(
        session.model_copy(
            update={"status": "running", "active_run_id": run.run_id, "last_run_id": run.run_id}
        )
    )
    first = store.append_ordered_event(
        EventRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="model_text_delta",
            payload={"text": "snapshot"},
        )
    )
    load_session = sqlite_store.read_session
    appended: list[EventRecord] = []
    injected = False

    def append_after_session_read(connection, session_id):
        nonlocal injected
        session = load_session(connection, session_id)
        if not injected:
            injected = True
            appended.append(
                store.append_ordered_event(
                    EventRecord(
                        session_id=session_id,
                        run_id=run.run_id,
                        event_type="diagnostic",
                        payload={"message": "concurrent event"},
                    )
                )
            )
        return session

    monkeypatch.setattr(sqlite_store, "read_session", append_after_session_read)

    snapshot = store.load_session_observation(session.session_id)
    replay, truncated = store.load_events_after(
        session.session_id,
        after_sequence=snapshot.event_high_water,
    )

    assert snapshot.event_high_water == first.sequence
    assert len(appended) == 1
    assert replay == appended
    assert truncated is False
