"""Small request headers and current-request lookup in the caller's transaction."""

from __future__ import annotations

import sqlite3

from ouro.core.context_snapshots import ModelRequestHeader
from ouro.core.errors import PersistenceError
from ouro.core.persistence import decode_versioned_record_json


def _decode(payload: str, session_id: str, context_id: str) -> ModelRequestHeader:
    header = decode_versioned_record_json(ModelRequestHeader, payload, label="request header")
    if header.session_id != session_id or header.context_id != context_id:
        raise PersistenceError("request header has a foreign identity")
    return header


def read_request_header(
    connection: sqlite3.Connection, session_id: str, context_id: str
) -> ModelRequestHeader | None:
    row = connection.execute(
        "SELECT payload FROM request_headers WHERE session_id = ? AND context_id = ?",
        (session_id, context_id),
    ).fetchone()
    return _decode(row[0], session_id, context_id) if row is not None else None


def read_latest_header(
    connection: sqlite3.Connection, session_id: str
) -> ModelRequestHeader | None:
    row = connection.execute(
        "SELECT b.context_id, h.payload FROM latest_contexts b "
        "LEFT JOIN request_headers h USING(session_id, context_id) WHERE b.session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    if row[1] is None:
        raise PersistenceError("latest context references a missing ordinary request")
    header = _decode(row[1], session_id, row[0])
    if header.purpose != "agent":
        raise PersistenceError("latest context references a missing ordinary request")
    return header


def read_headers(connection: sqlite3.Connection, session_id: str) -> list[ModelRequestHeader]:
    return [
        _decode(row[1], session_id, row[0])
        for row in connection.execute(
            "SELECT context_id, payload FROM request_headers "
            "WHERE session_id = ? ORDER BY context_id",
            (session_id,),
        )
    ]


def write_header(connection: sqlite3.Connection, header: ModelRequestHeader) -> None:
    connection.execute(
        "INSERT INTO request_headers VALUES (?, ?, ?) "
        "ON CONFLICT(session_id, context_id) DO UPDATE SET payload = excluded.payload",
        (header.session_id, header.context_id, header.model_dump_json()),
    )
