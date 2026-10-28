"""Request-owned current output, appended and retired by native transactions."""

from __future__ import annotations

import sqlite3

from pydantic import ValidationError

from ouro.core.context_snapshots import ModelRequestHeader
from ouro.core.errors import PersistenceError
from ouro.core.response_drafts import ResponseDraftChunk, ResponseDraftFragment, ResponseDraftPage

DRAFT_SCHEMA = """
CREATE TABLE IF NOT EXISTS response_streams (
    session_id TEXT PRIMARY KEY,
    context_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS response_drafts (
    session_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    channel TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY(session_id, position),
    FOREIGN KEY(session_id) REFERENCES response_streams(session_id) ON DELETE CASCADE
);
"""


def retire_draft(connection: sqlite3.Connection, session_id: str, context_id: str) -> None:
    connection.execute(
        "DELETE FROM response_streams WHERE session_id = ? AND context_id = ?",
        (session_id, context_id),
    )


def publish_draft(connection: sqlite3.Connection, header: ModelRequestHeader) -> None:
    if header.purpose != "agent":
        return
    if header.response_closed_at is not None:
        retire_draft(connection, header.session_id, header.context_id)
    elif header.started_at is not None:
        connection.execute(
            "INSERT INTO response_streams VALUES (?, ?) ON CONFLICT(session_id) DO NOTHING",
            (header.session_id, header.context_id),
        )


def append_chunk(
    connection: sqlite3.Connection, session_id: str, context_id: str, chunk: ResponseDraftChunk
) -> int:
    row = connection.execute(
        "INSERT INTO response_drafts(session_id, position, channel, content) "
        "SELECT session_id, (SELECT COALESCE(MAX(position), 0) + 1 FROM response_drafts "
        "WHERE session_id = ?), ?, ? FROM response_streams "
        "WHERE session_id = ? AND context_id = ? RETURNING position",
        (session_id, chunk.channel, chunk.content, session_id, context_id),
    ).fetchone()
    if row is None:
        raise PersistenceError("request no longer accepts response Draft fragments")
    return int(row[0])


def read_page(
    connection: sqlite3.Connection,
    session_id: str,
    context_id: str,
    *,
    after_cursor: int,
    limit: int | None,
) -> ResponseDraftPage | None:
    owner = connection.execute(
        "SELECT context_id FROM response_streams WHERE session_id = ?", (session_id,)
    ).fetchone()
    if owner is None or owner[0] != context_id:
        return None
    rows = connection.execute(
        "SELECT position, channel, content FROM response_drafts "
        "WHERE session_id = ? AND position > ? ORDER BY position LIMIT ?",
        (session_id, after_cursor, limit if limit is not None else -1),
    )
    try:
        chunks = tuple(
            ResponseDraftFragment(cursor=row[0], channel=row[1], content=row[2]) for row in rows
        )
    except ValidationError as exc:
        raise PersistenceError("invalid response Draft fragment") from exc
    return ResponseDraftPage(
        context_id=context_id, chunks=chunks, cursor=chunks[-1].cursor if chunks else after_cursor
    )
