"""Native record access for persistence corruption and transaction fault tests."""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager

from ouro.sessions import LocalSessionStore


@contextmanager
def session_connection(store, session_id):
    database = (
        store.sessions_dir / session_id / "session-data.sqlite3"
        if isinstance(store, LocalSessionStore)
        else store.database
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        yield connection


def read_session_payload(store, session_id):
    with session_connection(store, session_id) as connection:
        return connection.execute(
            "SELECT payload FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()[0]


def write_session_payload(store, session_id, payload):
    with session_connection(store, session_id) as connection:
        connection.execute(
            "UPDATE sessions SET payload = ? WHERE session_id = ?", (payload, session_id)
        )
