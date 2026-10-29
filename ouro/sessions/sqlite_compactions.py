from __future__ import annotations

import sqlite3

from ouro.core.compaction import CompactionRecord, CompactionReference


def read_references(connection: sqlite3.Connection, session_id: str) -> list[CompactionReference]:
    return [
        CompactionReference(
            compaction_id=identity,
            previous_compaction_id=previous,
            covered_end_message_id=covered,
            history_boundary_message_id=boundary,
        )
        for identity, previous, covered, boundary in connection.execute(
            "SELECT compaction_id, previous_compaction_id, covered_end_message_id, "
            "history_boundary_message_id FROM compactions WHERE session_id = ? ORDER BY row_id",
            (session_id,),
        )
    ]


def insert_compaction(
    connection: sqlite3.Connection, record: CompactionRecord, *, revision: int = 0
) -> None:
    connection.execute(
        "INSERT INTO compactions(session_id, compaction_id, previous_compaction_id, "
        "covered_end_message_id, history_boundary_message_id, payload, modified_revision) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (
            record.session_id,
            record.compaction_id,
            record.previous_compaction_id,
            record.covered_end_message_id,
            record.history_boundary_message_id,
            record.model_dump_json(),
            revision,
        ),
    )


def update_compaction_boundary(connection: sqlite3.Connection, record: CompactionRecord) -> None:
    connection.execute(
        "UPDATE compactions SET history_boundary_message_id = ?, payload = ? "
        "WHERE session_id = ? AND compaction_id = ?",
        (
            record.history_boundary_message_id,
            record.model_dump_json(),
            record.session_id,
            record.compaction_id,
        ),
    )
