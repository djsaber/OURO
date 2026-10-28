"""Message publication and its incremental Run counts in the caller's transaction."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence

from ouro.core.errors import PersistenceError
from ouro.core.message_statistics import (
    RunMessageCounts,
    RunResultReferences,
    collect_run_result_references,
    delegation_input_id,
    message_call_keys,
    message_input_origin,
)
from ouro.core.messages import MessageRecord


def read_counts(connection: sqlite3.Connection, session_id: str, run_id: str) -> RunMessageCounts:
    row = connection.execute(
        "SELECT tool_call_count, input_count, automatic_input_count FROM run_message_counts "
        "WHERE session_id = ? AND run_id = ?",
        (session_id, run_id),
    ).fetchone()
    if row is None:
        if (
            connection.execute(
                "SELECT 1 FROM messages WHERE session_id = ? AND run_id = ? LIMIT 1",
                (session_id, run_id),
            ).fetchone()
            is not None
        ):
            raise PersistenceError("published Run messages have no counts")
        return RunMessageCounts(run_id=run_id)
    return RunMessageCounts(
        run_id=run_id, tool_call_count=row[0], input_count=row[1], automatic_input_count=row[2]
    )


def _change_counts(
    connection: sqlite3.Connection,
    session_id: str,
    run_id: str,
    *,
    calls: int = 0,
    inputs: int = 0,
    automatic: int = 0,
) -> None:
    cursor = connection.execute(
        "UPDATE run_message_counts SET tool_call_count = tool_call_count + ?, "
        "input_count = input_count + ?, automatic_input_count = automatic_input_count + ? "
        "WHERE session_id = ? AND run_id = ?",
        (calls, inputs, automatic, session_id, run_id),
    )
    if cursor.rowcount == 0:
        connection.execute(
            "INSERT INTO run_message_counts VALUES (?, ?, ?, ?, ?)",
            (session_id, run_id, calls, inputs, automatic),
        )


def read_result_references(
    connection: sqlite3.Connection, session_id: str, run_id: str, *, delegation_id: str
) -> RunResultReferences:
    rows = connection.execute(
        "SELECT message_id, role, delegation_input_id, resource_refs FROM messages "
        "WHERE session_id = ? AND run_id = ? ORDER BY row_id",
        (session_id, run_id),
    )
    return collect_run_result_references(
        ((row[0], row[1], row[2], json.loads(row[3])) for row in rows),
        delegation_id=delegation_id,
    )


def write_messages(
    connection: sqlite3.Connection,
    messages: Sequence[MessageRecord],
    *,
    resource_refs: Mapping[str, tuple[str, ...]] | None = None,
) -> None:
    """Publish one batch and update each affected Run's counts once."""
    connection.executemany(
        "INSERT INTO messages(session_id, message_id, sequence, run_id, input_origin, "
        "role, delegation_input_id, resource_refs, payload) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                message.session_id,
                message.message_id,
                message.sequence,
                message.run_id,
                message_input_origin(message),
                message.role,
                delegation_input_id(message),
                json.dumps(
                    message.resource_refs
                    if resource_refs is None
                    else resource_refs[message.message_id]
                ),
                message.storage_json(),
            )
            for message in messages
        ),
    )
    counts: dict[tuple[str, str], RunMessageCounts] = {}
    for message in messages:
        if message.run_id is None:
            continue
        identity = (message.session_id, message.run_id)
        if identity not in counts:
            counts[identity] = RunMessageCounts(run_id=message.run_id)
        count = counts[identity]
        origin = message_input_origin(message)
        count.input_count += int(origin is not None)
        count.automatic_input_count += int(origin == "automatic")
        keys = message_call_keys(message)
        if keys:
            encoded = json.dumps(keys)
            count.tool_call_count += connection.execute(
                "SELECT COUNT(*) FROM json_each(?) AS requested WHERE NOT EXISTS "
                "(SELECT 1 FROM message_call_references "
                "WHERE session_id = ? AND call_key = requested.value)",
                (encoded, message.session_id),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO message_call_references "
                "SELECT m.row_id, m.session_id, m.run_id, requested.value "
                "FROM messages AS m CROSS JOIN json_each(?) AS requested "
                "WHERE m.session_id = ? AND m.message_id = ?",
                (encoded, message.session_id, message.message_id),
            )
    for (session_id, run_id), count in counts.items():
        _change_counts(
            connection,
            session_id,
            run_id,
            calls=count.tool_call_count,
            inputs=count.input_count,
            automatic=count.automatic_input_count,
        )


def truncate_messages(connection: sqlite3.Connection, session_id: str, cut_row: int) -> None:
    inputs = connection.execute(
        "SELECT run_id, COUNT(input_origin), SUM(input_origin = 'automatic') "
        "FROM messages WHERE session_id = ? AND row_id >= ? AND run_id IS NOT NULL GROUP BY run_id",
        (session_id, cut_row),
    ).fetchall()
    keys = connection.execute(
        "SELECT DISTINCT run_id, call_key FROM message_call_references "
        "WHERE session_id = ? AND message_row >= ?",
        (session_id, cut_row),
    ).fetchall()
    connection.execute(
        "DELETE FROM messages WHERE session_id = ? AND row_id >= ?", (session_id, cut_row)
    )
    for run_id, count, automatic in inputs:
        _change_counts(connection, session_id, run_id, inputs=-count, automatic=-(automatic or 0))
    for run_id, key in keys:
        if (
            connection.execute(
                "SELECT 1 FROM message_call_references "
                "WHERE session_id = ? AND call_key = ? LIMIT 1",
                (session_id, key),
            ).fetchone()
            is None
        ):
            _change_counts(connection, session_id, run_id, calls=-1)
