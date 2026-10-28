"""Native resource ownership records shared by both Session backends."""

from __future__ import annotations

import sqlite3

from ouro.core.errors import PersistenceError, ResourceNotFoundError
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.resources import SessionResourceReference, validate_resource_id

RESOURCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS resource_references (
    session_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(session_id, resource_id),
    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);
"""


def read_references(
    connection: sqlite3.Connection, session_id: str
) -> list[SessionResourceReference]:
    return [
        decode_versioned_record_json(SessionResourceReference, row[0], label="resource reference")
        for row in connection.execute(
            "SELECT payload FROM resource_references WHERE session_id = ? ORDER BY resource_id",
            (session_id,),
        )
    ]


def read_reference(
    connection: sqlite3.Connection, session_id: str, resource_id: str
) -> SessionResourceReference:
    validate_resource_id(resource_id)
    row = connection.execute(
        "SELECT payload FROM resource_references WHERE session_id = ? AND resource_id = ?",
        (session_id, resource_id),
    ).fetchone()
    if row is None:
        raise ResourceNotFoundError(
            f"resource {resource_id} is not attached to session {session_id}"
        )
    reference = decode_versioned_record_json(
        SessionResourceReference, row[0], label="resource reference"
    )
    if reference.resource_id != resource_id:
        raise PersistenceError("resource record has a foreign identity")
    return reference


def insert_reference(
    connection: sqlite3.Connection, session_id: str, reference: SessionResourceReference
) -> bool:
    validate_resource_id(reference.resource_id)
    row = connection.execute(
        "SELECT payload FROM resource_references WHERE session_id = ? AND resource_id = ?",
        (session_id, reference.resource_id),
    ).fetchone()
    if row is not None:
        existing = decode_versioned_record_json(
            SessionResourceReference, row[0], label="resource reference"
        )
        if existing != reference:
            raise PersistenceError("resource identity is already bound to another record")
        return False
    connection.execute(
        "INSERT INTO resource_references(session_id, resource_id, payload) VALUES(?, ?, ?)",
        (session_id, reference.resource_id, reference.model_dump_json()),
    )
    return True


def attach_reference(
    connection: sqlite3.Connection, session_id: str, reference: SessionResourceReference
) -> None:
    if insert_reference(connection, session_id, reference):
        connection.execute(
            "UPDATE sessions SET payload = json_set(payload, '$.resource_count', "
            "json_extract(payload, '$.resource_count') + 1) WHERE session_id = ?",
            (session_id,),
        )


def remove_reference(connection: sqlite3.Connection, session_id: str, resource_id: str) -> None:
    validate_resource_id(resource_id)
    cursor = connection.execute(
        "DELETE FROM resource_references WHERE session_id = ? AND resource_id = ?",
        (session_id, resource_id),
    )
    if cursor.rowcount == 0:
        raise ResourceNotFoundError(
            f"resource {resource_id} is not attached to session {session_id}"
        )
    connection.execute(
        "UPDATE sessions SET payload = json_set(payload, '$.resource_count', "
        "json_extract(payload, '$.resource_count') - 1) WHERE session_id = ?",
        (session_id,),
    )
