"""Latest fact positions, without retaining a history of changes."""

from __future__ import annotations

import sqlite3

from ouro.core.compaction import CompactionRecord
from ouro.core.inputs import InputState
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.state import RunRecord
from ouro.core.tool_batches import ToolCallState
from ouro.sessions.observation import FactCursor, SessionFactPage

OBSERVATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_revisions (
    session_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS compactions_boundary
    ON compactions(session_id, history_boundary_message_id);
CREATE INDEX IF NOT EXISTS runs_modified ON runs(session_id, modified_revision, run_id);
CREATE INDEX IF NOT EXISTS calls_modified
    ON tool_call_states(
        session_id, modified_revision, (run_id || ':' || batch_id || ':' || tool_call_id)
    );
CREATE INDEX IF NOT EXISTS inputs_modified
    ON input_states(session_id, modified_revision, input_id);
CREATE INDEX IF NOT EXISTS compactions_modified
    ON compactions(session_id, modified_revision, compaction_id);
"""


def read_revision(connection: sqlite3.Connection, session_id: str) -> int:
    row = connection.execute(
        "SELECT revision FROM session_revisions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return row[0] if row is not None else 0


def advance_revision(connection: sqlite3.Connection, session_id: str) -> int:
    """Advance once inside the transaction that publishes a set of facts."""
    return connection.execute(
        "INSERT INTO session_revisions(session_id, revision) VALUES (?, 1) "
        "ON CONFLICT(session_id) DO UPDATE SET revision = revision + 1 RETURNING revision",
        (session_id,),
    ).fetchone()[0]


def read_changes(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    after_revision: int,
    through_revision: int,
    limit: int,
    cursor: FactCursor | None = None,
) -> SessionFactPage:
    """Each object appears once at its latest position, including ties within a commit.

    A concurrently modified object moves beyond ``through_revision`` and is
    picked up by the next pass. No historical copy or change list is retained.
    """
    position = (
        (cursor.revision, cursor.kind, cursor.identity)
        if cursor is not None
        else (after_revision, 4, "")
    )
    queries = []
    parameters: list[object] = []
    for kind, table, identity in (
        (0, "runs", "run_id"),
        (1, "input_states", "input_id"),
        (2, "tool_call_states", "run_id || ':' || batch_id || ':' || tool_call_id"),
        (3, "compactions", "compaction_id"),
    ):
        queries.append(
            f"SELECT * FROM (SELECT modified_revision, {kind} AS kind, "
            f"{identity} AS identity, payload "
            f"FROM {table} WHERE session_id = ? "
            "AND modified_revision >= ? AND modified_revision <= ? "
            f"AND (modified_revision, {kind}, {identity}) > (?, ?, ?) "
            "ORDER BY modified_revision, identity LIMIT ?)"
        )
        parameters.extend((session_id, position[0], through_revision, *position, limit + 1))
    rows = connection.execute(
        " UNION ALL ".join(queries) + " ORDER BY modified_revision, kind, identity LIMIT ?",
        (*parameters, limit + 1),
    ).fetchall()
    selected = rows[:limit]
    records: tuple[list, list, list, list] = ([], [], [], [])
    types = (RunRecord, InputState, ToolCallState, CompactionRecord)
    for _, kind, _, payload in selected:
        records[kind].append(
            decode_versioned_record_json(types[kind], payload, label="observed fact")
        )
    last = selected[-1] if len(rows) > limit else None
    return SessionFactPage.model_construct(
        revision=through_revision,
        runs=tuple(records[0]),
        inputs=tuple(records[1]),
        calls=tuple(records[2]),
        compactions=tuple(records[3]),
        next_cursor=(
            FactCursor(revision=last[0], kind=last[1], identity=last[2]) if last else None
        ),
    )
