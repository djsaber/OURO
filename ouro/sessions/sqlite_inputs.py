from __future__ import annotations

import sqlite3
from collections.abc import Collection, Iterator
from itertools import batched
from typing import Literal

from ouro.core.errors import PersistenceError
from ouro.core.inputs import InputRecord, InputState, QueuedInput
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.session_commits import SessionCommit
from ouro.core.state import SessionRecord

INPUT_SCHEMA = """CREATE TABLE IF NOT EXISTS inputs (
    session_id TEXT NOT NULL,
    input_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(session_id, input_id),
    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);"""


def _decode_input(payload: str | bytes, session_id: str, input_id: str) -> InputRecord:
    record = decode_versioned_record_json(InputRecord, payload, label=f"input {input_id}")
    if record.session_id != session_id or record.item.queue_id != input_id:
        raise PersistenceError("accepted input has a foreign identity")
    return record


def _rows(
    connection: sqlite3.Connection,
    table: Literal["inputs", "input_states"],
    session_id: str,
    input_ids: Collection[str] | None,
) -> Iterator[tuple[str, str]]:
    query = f"SELECT input_id, payload FROM {table} WHERE session_id = ?"
    if input_ids is None:
        yield from connection.execute(query + " ORDER BY input_id", (session_id,))
    else:
        for selected in batched(input_ids, 256, strict=False):
            parameters = ",".join("?" for _ in selected)
            yield from connection.execute(
                query + f" AND input_id IN ({parameters})", (session_id, *selected)
            )


def read_input(
    connection: sqlite3.Connection, session_id: str, input_id: str
) -> InputRecord | None:
    return next(read_inputs(connection, session_id, (input_id,)), None)


def read_preview_input(
    connection: sqlite3.Connection, session_id: str, input_id: str, *, byte_limit: int
) -> tuple[InputRecord, int] | None:
    row = connection.execute(
        "SELECT rowid FROM inputs WHERE session_id = ? AND input_id = ?", (session_id, input_id)
    ).fetchone()
    if row is None:
        raise PersistenceError("message references a missing Input")
    with connection.blobopen("inputs", "payload", row[0], readonly=True) as payload:
        size = len(payload)
        if size > byte_limit:
            return None
        return _decode_input(payload.read(), session_id, input_id), size


def read_inputs(
    connection: sqlite3.Connection, session_id: str, input_ids: Collection[str] | None = None
) -> Iterator[InputRecord]:
    for identity, payload in _rows(connection, "inputs", session_id, input_ids):
        yield _decode_input(payload, session_id, identity)


def read_state(connection: sqlite3.Connection, session_id: str, input_id: str) -> InputState | None:
    return next(read_states(connection, session_id, (input_id,)), None)


def read_states(
    connection: sqlite3.Connection, session_id: str, input_ids: Collection[str] | None = None
) -> Iterator[InputState]:
    for identity, payload in _rows(connection, "input_states", session_id, input_ids):
        state = decode_versioned_record_json(InputState, payload, label=f"input state {identity}")
        if state.session_id != session_id or state.input_id != identity:
            raise PersistenceError("input state has a foreign identity")
        yield state


def read_commit_input_states(
    connection: sqlite3.Connection,
    request: SessionCommit,
    session: SessionRecord,
    withdrawn_ids: tuple[str, ...],
) -> tuple[InputState, ...]:
    identities = [record.item.queue_id for record in request.inputs]
    identities.extend(change.input_id for change in request.input_changes)
    identities.extend(withdrawn_ids)
    if request.before_message_id is not None:
        identities.extend(session.pending_input_ids)
    return tuple(read_states(connection, session.session_id, dict.fromkeys(identities)))


def _omitted_fields(value: QueuedInput | InputState) -> set[str]:
    """Only omit declared routing defaults, never nested attachment or plugin values."""
    return {
        name
        for name, field in type(value).model_fields.items()
        if name not in {"schema_version", "status"} and getattr(value, name) == field.default
    }


def write_input(connection: sqlite3.Connection, record: InputRecord) -> None:
    connection.execute(
        "INSERT INTO inputs(session_id, input_id, payload) VALUES (?, ?, ?)",
        (
            record.session_id,
            record.item.queue_id,
            record.model_dump_json(exclude={"item": _omitted_fields(record.item)}),
        ),
    )


def write_state(connection: sqlite3.Connection, state: InputState, *, revision: int = 0) -> None:
    connection.execute(
        "INSERT INTO input_states(session_id, input_id, payload, modified_revision) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(session_id, input_id) DO UPDATE SET payload = excluded.payload, "
        "modified_revision = excluded.modified_revision",
        (
            state.session_id,
            state.input_id,
            state.model_dump_json(exclude=_omitted_fields(state)),
            revision,
        ),
    )
