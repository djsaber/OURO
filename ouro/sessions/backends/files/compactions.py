from __future__ import annotations

import sqlite3
from pathlib import Path

from ouro.core.compaction import CompactionRecord
from ouro.core.errors import PersistenceError
from ouro.core.persistence import decode_versioned_record_json


def has_compaction(connection: sqlite3.Connection, directory: Path, identity: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM compactions WHERE session_id = ? AND compaction_id = ?",
            (directory.name, identity),
        ).fetchone()
        is not None
    )


def _record(directory: Path, identity: str, payload: str) -> CompactionRecord:
    record = decode_versioned_record_json(CompactionRecord, payload, label="compaction")
    if record.compaction_id != identity or record.session_id != directory.name:
        raise PersistenceError("compaction record has a foreign identity")
    return record


def read_compaction(
    connection: sqlite3.Connection, directory: Path, identity: str
) -> CompactionRecord:
    row = connection.execute(
        "SELECT payload FROM compactions WHERE session_id = ? AND compaction_id = ?",
        (directory.name, identity),
    ).fetchone()
    if row is None:
        raise FileNotFoundError(identity)
    return _record(directory, identity, row[0])


def read_compactions(connection: sqlite3.Connection, directory: Path) -> list[CompactionRecord]:
    return [
        _record(directory, identity, payload)
        for identity, payload in connection.execute(
            "SELECT compaction_id, payload FROM compactions WHERE session_id = ? ORDER BY row_id",
            (directory.name,),
        )
    ]
