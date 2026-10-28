"""Canonical session data and message locations inside one Files Session."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ouro.core.errors import PersistenceError
from ouro.sessions.sqlite_connections import ExecutionConnections
from ouro.sessions.sqlite_drafts import DRAFT_SCHEMA
from ouro.sessions.sqlite_events import EVENT_SCHEMA
from ouro.sessions.sqlite_resources import RESOURCE_SCHEMA

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    history_epoch INTEGER NOT NULL,
    needs_recovery INTEGER NOT NULL,
    model_round_count INTEGER NOT NULL,
    summary_request_count INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(session_id, run_id),
    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS runs_request_counts
    ON runs(session_id, run_id, model_round_count, summary_request_count);
CREATE INDEX IF NOT EXISTS runs_pending
    ON runs(session_id, history_epoch, started_at, run_id) WHERE needs_recovery = 1;
CREATE TABLE IF NOT EXISTS locations (
    position INTEGER PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE,
    offset INTEGER NOT NULL,
    role TEXT NOT NULL,
    run_id TEXT,
    queued_input_id TEXT,
    assistant_turn_id TEXT,
    context_delivery_key TEXT,
    input_origin TEXT,
    delegation_input_id TEXT,
    resource_refs TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS call_memberships (
    call_key TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (call_key, position)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS call_positions ON call_memberships(position);
CREATE TABLE IF NOT EXISTS counts (run_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS run_locations
ON locations(run_id, position) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS input_locations
ON locations(queued_input_id, position) WHERE queued_input_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS assistant_turns
ON locations(run_id, assistant_turn_id, position) WHERE assistant_turn_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS context_deliveries
ON locations(run_id, context_delivery_key, position) WHERE context_delivery_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS assistant_runs
ON locations(run_id, position) WHERE role = 'assistant';
CREATE INDEX IF NOT EXISTS input_messages ON locations(position)
WHERE role IN ('user', 'system');
CREATE TABLE IF NOT EXISTS tool_calls (
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    projection TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(session_id, run_id, batch_id, tool_call_id)
);
CREATE TABLE IF NOT EXISTS tool_call_starts (
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(session_id, run_id, batch_id, tool_call_id, attempt),
    FOREIGN KEY(session_id, run_id, batch_id, tool_call_id)
        REFERENCES tool_calls(session_id, run_id, batch_id, tool_call_id)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS tool_outcomes (
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    payload TEXT NOT NULL,
    structured TEXT NOT NULL,
    PRIMARY KEY(session_id, run_id, batch_id, tool_call_id, attempt),
    FOREIGN KEY(session_id, run_id, batch_id, tool_call_id)
        REFERENCES tool_calls(session_id, run_id, batch_id, tool_call_id)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS tool_call_states (
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(session_id, run_id, batch_id, tool_call_id),
    FOREIGN KEY(session_id, run_id, batch_id, tool_call_id)
        REFERENCES tool_calls(session_id, run_id, batch_id, tool_call_id)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS compactions (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    compaction_id TEXT NOT NULL UNIQUE,
    previous_compaction_id TEXT,
    covered_end_message_id TEXT NOT NULL,
    history_boundary_message_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS compaction_references
    ON compactions(session_id, row_id, compaction_id,
        previous_compaction_id, covered_end_message_id,
        history_boundary_message_id);
CREATE TABLE IF NOT EXISTS request_headers (
    session_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(session_id, context_id)
);
CREATE TABLE IF NOT EXISTS latest_contexts (
    session_id TEXT PRIMARY KEY,
    context_id TEXT NOT NULL
);
"""


@contextmanager
def connect_data(
    directory: Path, *, mode: str = "ro", connections: ExecutionConnections | None = None
) -> Iterator[sqlite3.Connection]:
    # Read scopes also need an ordinary handle: if the Run writer exits first,
    # the last observer must be able to checkpoint and remove the native WAL.
    access = "rw" if mode == "ro" else mode
    uri = (directory / "session-data.sqlite3").absolute().as_uri() + f"?mode={access}"
    try:
        connection = (
            connections.borrow() if connections is not None else sqlite3.connect(uri, uri=True)
        )
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            if mode != "ro":
                connection.execute("PRAGMA synchronous = FULL")
            with connection:
                if mode == "ro":
                    connection.execute("BEGIN")
                yield connection
        finally:
            if connections is None:
                connection.close()
            else:
                connections.return_connection(connection)
    except sqlite3.Error as exc:
        raise PersistenceError(f"could not access Session data: {exc}") from exc


def initialize_index(connection: sqlite3.Connection) -> None:
    """Replace an unpublished snapshot index in one transaction."""
    # Completed output lives in JSONL; reclaim the database pages owned by its
    # temporary Draft in the lifecycle transaction instead of keeping its peak.
    connection.execute("PRAGMA auto_vacuum = FULL")
    connection.execute("PRAGMA journal_mode = WAL")
    # Schema creation is independent of publishing a Session. Keep the empty
    # schema if building a reserved snapshot fails, so the same input can retry.
    connection.executescript(
        "BEGIN;\n" + _SCHEMA + DRAFT_SCHEMA + EVENT_SCHEMA + RESOURCE_SCHEMA + "COMMIT;\nBEGIN;"
    )
    for table in (
        "sessions",
        "locations",
        "call_memberships",
        "counts",
        "tool_calls",
        "compactions",
        "response_streams",
        "request_headers",
        "latest_contexts",
    ):
        connection.execute(f"DELETE FROM {table}")
