"""Canonical events and indexed observation cursors for native session storage."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from ouro.core.events import EventRecord
from ouro.core.persistence import decode_versioned_record_json

EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL,
    session_id TEXT GENERATED ALWAYS AS (json_extract(payload, '$.session_id')) VIRTUAL NOT NULL,
    event_id TEXT GENERATED ALWAYS AS (json_extract(payload, '$.event_id')) VIRTUAL NOT NULL UNIQUE,
    run_id TEXT GENERATED ALWAYS AS (json_extract(payload, '$.run_id')) VIRTUAL,
    sequence INTEGER GENERATED ALWAYS AS (json_extract(payload, '$.sequence')) VIRTUAL NOT NULL,
    UNIQUE(session_id, sequence),
    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS events_session_order ON events(session_id, row_id);
CREATE INDEX IF NOT EXISTS events_session_run_order ON events(session_id, run_id, row_id);
"""


def insert_events(connection: sqlite3.Connection, events: Iterable[EventRecord]) -> None:
    connection.executemany(
        "INSERT INTO events(payload) VALUES(?)",
        ((event.storage_json(),) for event in events),
    )


def latest_sequence(connection: sqlite3.Connection, session_id: str) -> int:
    return int(
        connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
    )


def read_events(connection: sqlite3.Connection, session_id: str) -> list[EventRecord]:
    return [
        decode_versioned_record_json(EventRecord, row[0], label="event")
        for row in connection.execute(
            "SELECT payload FROM events WHERE session_id = ? ORDER BY row_id", (session_id,)
        )
    ]


def read_page(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    cursor: int,
    limit: int,
    run_id: str | None,
) -> tuple[list[EventRecord], int | None]:
    parameters: tuple[str | int, ...] = (session_id, cursor)
    run_clause = ""
    if run_id is not None:
        run_clause = " AND run_id = ?"
        parameters += (run_id,)
    rows = connection.execute(
        "SELECT row_id, payload FROM events WHERE session_id = ? AND row_id > ?"
        + run_clause
        + " ORDER BY row_id LIMIT ?",
        (*parameters, limit + 1),
    ).fetchall()
    return (
        [decode_versioned_record_json(EventRecord, row[1], label="event") for row in rows[:limit]],
        int(rows[limit - 1][0]) if len(rows) > limit else None,
    )


def read_after(
    connection: sqlite3.Connection, session_id: str, *, after_sequence: int, limit: int
) -> tuple[list[EventRecord], bool]:
    rows = connection.execute(
        "SELECT payload FROM events WHERE session_id = ? AND sequence > ? "
        "ORDER BY sequence LIMIT ?",
        (session_id, after_sequence, limit + 1),
    ).fetchall()
    return (
        [decode_versioned_record_json(EventRecord, row[0], label="event") for row in rows[:limit]],
        len(rows) > limit,
    )
