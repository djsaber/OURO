"""Canonical Session heads shared by the native stores."""

from __future__ import annotations

import sqlite3

from ouro.core.errors import PersistenceError
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.state import SessionRecord


def require_session(connection: sqlite3.Connection, session_id: str) -> None:
    if (
        connection.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        is None
    ):
        raise FileNotFoundError(session_id)


def read_session(connection: sqlite3.Connection, session_id: str) -> SessionRecord:
    row = connection.execute(
        "SELECT payload FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise FileNotFoundError(session_id)
    session = decode_versioned_record_json(SessionRecord, row[0], label="Session")
    if session.session_id != session_id:
        raise PersistenceError("Session record has a foreign identity")
    return session


def write_session(connection: sqlite3.Connection, session: SessionRecord) -> None:
    # The current controls projection is published with its Harness reference.
    connection.execute(
        "INSERT INTO sessions VALUES (?, ?) ON CONFLICT(session_id) "
        "DO UPDATE SET payload = excluded.payload",
        (session.session_id, session.model_dump_json()),
    )
