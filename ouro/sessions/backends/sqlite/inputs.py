from __future__ import annotations

import sqlite3

from ouro.core.errors import PersistenceError
from ouro.core.inputs import InputRecord, InputState
from ouro.core.persistence import decode_versioned_record_json


def read_input(
    connection: sqlite3.Connection, session_id: str, input_id: str
) -> InputRecord | None:
    row = connection.execute(
        "SELECT payload FROM inputs WHERE session_id = ? AND input_id = ?", (session_id, input_id)
    ).fetchone()
    if row is None:
        return None
    record = decode_versioned_record_json(InputRecord, row[0], label=f"input {input_id}")
    if record.session_id != session_id or record.item.queue_id != input_id:
        raise PersistenceError("accepted input has a foreign identity")
    return record


def read_state(connection: sqlite3.Connection, session_id: str, input_id: str) -> InputState | None:
    row = connection.execute(
        "SELECT payload FROM input_states WHERE session_id = ? AND input_id = ?",
        (session_id, input_id),
    ).fetchone()
    if row is None:
        return None
    record = decode_versioned_record_json(InputState, row[0], label=f"input state {input_id}")
    if record.session_id != session_id or record.input_id != input_id:
        raise PersistenceError("input state has a foreign identity")
    return record


def write_input(connection: sqlite3.Connection, record: InputRecord) -> None:
    connection.execute(
        "INSERT INTO inputs(session_id, input_id, payload) VALUES (?, ?, ?)",
        (record.session_id, record.item.queue_id, record.model_dump_json()),
    )


def write_state(connection: sqlite3.Connection, state: InputState) -> None:
    connection.execute(
        "INSERT INTO input_states(session_id, input_id, payload) VALUES (?, ?, ?) "
        "ON CONFLICT(session_id, input_id) DO UPDATE SET payload = excluded.payload",
        (state.session_id, state.input_id, state.model_dump_json()),
    )
