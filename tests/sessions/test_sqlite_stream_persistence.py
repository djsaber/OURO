import sqlite3

import pytest

from ouro.core.events import EventRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import SQLiteSessionStore


@pytest.mark.parametrize(
    "event_type,payload",
    (
        ("model_text_delta", {"text": "hello"}),
        ("model_reasoning_delta", {"reasoning_content": "thinking"}),
        (
            "model_tool_call_delta",
            {
                "index": 0,
                "id_delta": "call_1",
                "name_delta": "Read",
                "arguments_delta": '{"path":"README.md"}',
            },
        ),
    ),
)
def test_sqlite_stream_deltas_are_visible_with_group_durability(
    tmp_path,
    monkeypatch,
    event_type: str,
    payload: dict[str, object],
) -> None:
    store = SQLiteSessionStore(tmp_path / "ouro.db")
    store.initialize()
    session = SessionRecord(title="SQLite stream durability")
    store.create_session(session)
    synchronous_modes: list[tuple[str, int]] = []
    insert_event = store._insert_event

    def capture_synchronous_mode(
        connection: sqlite3.Connection,
        event: EventRecord,
    ) -> None:
        mode = connection.execute("PRAGMA synchronous").fetchone()[0]
        synchronous_modes.append((event.event_type, int(mode)))
        insert_event(connection, event)

    monkeypatch.setattr(store, "_insert_event", capture_synchronous_mode)

    delta = store.append_ordered_event(
        EventRecord(
            session_id=session.session_id,
            event_type=event_type,
            payload=payload,
        )
    )

    # NORMAL changes only the fsync policy. The committed delta is already
    # visible to another connection and retains its canonical sequence.
    visible = store.load_events_after(session.session_id, after_sequence=0)[0]
    assert [(event.event_id, event.sequence) for event in visible] == [(delta.event_id, 1)]

    boundary = store.append_ordered_event(
        EventRecord(
            session_id=session.session_id,
            event_type="model_message_completed",
        )
    )

    assert boundary.sequence == 2
    assert synchronous_modes == [
        (event_type, 1),  # SQLITE_SYNC_NORMAL
        ("model_message_completed", 2),  # SQLITE_SYNC_FULL
    ]


def test_outer_durable_transaction_never_downgrades_for_nested_delta(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteSessionStore(tmp_path / "ouro.db")
    store.initialize()
    session = SessionRecord(title="Nested SQLite durability")
    store.create_session(session)
    synchronous_modes: list[int] = []
    insert_event = store._insert_event

    def capture_synchronous_mode(
        connection: sqlite3.Connection,
        event: EventRecord,
    ) -> None:
        synchronous_modes.append(int(connection.execute("PRAGMA synchronous").fetchone()[0]))
        insert_event(connection, event)

    monkeypatch.setattr(store, "_insert_event", capture_synchronous_mode)

    with store.session_mutation(session.session_id):
        store.append_ordered_event(
            EventRecord(
                session_id=session.session_id,
                event_type="model_text_delta",
                payload={"text": "nested"},
            )
        )

    assert synchronous_modes == [2]


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
    original_load_models = store._load_models
    appended: list[EventRecord] = []

    def append_between_event_rows_and_high_water(connection, query, parameters, model_type):
        records = original_load_models(connection, query, parameters, model_type)
        if model_type is EventRecord and not appended:
            appended.append(
                store.append_ordered_event(
                    EventRecord(
                        session_id=session.session_id,
                        run_id=run.run_id,
                        event_type="model_text_delta",
                        payload={"text": "replay"},
                    )
                )
            )
        return records

    monkeypatch.setattr(store, "_load_models", append_between_event_rows_and_high_water)

    snapshot = store.load_session_observation(session.session_id)
    replay, truncated = store.load_events_after(
        session.session_id,
        after_sequence=snapshot.event_high_water,
    )

    assert snapshot.active_run_events == [first]
    assert snapshot.event_high_water == first.sequence
    assert replay == appended
    assert truncated is False
