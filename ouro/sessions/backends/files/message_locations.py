"""Transactional locations for the canonical JSONL message journal.

The Session lock and commit.json own publication across the journal and index.
SQLite supplies the ordered lookup structure; message bodies remain in JSONL.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Literal

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.errors import PersistenceError
from ouro.core.message_roles import MessageRole
from ouro.core.message_statistics import (
    InputOrigin,
    RunMessageCounts,
    delegation_input_id,
    message_call_keys,
    message_input_origin,
)
from ouro.core.messages import MessageRecord

LookupKind = Literal[
    "runs",
    "inputs",
    "assistant_turns",
    "context_deliveries",
    "assistant_runs",
    "input_messages",
    "calls",
]


class FileMessageReference(OuroModel):
    message_id: str
    position: int = Field(ge=1)
    offset: int = Field(ge=0)
    role: MessageRole
    run_id: str | None = None
    queued_input_id: str | None = None
    assistant_turn_id: str | None = None
    context_delivery_key: str | None = None
    call_keys: tuple[str, ...] = ()
    input_origin: InputOrigin | None = None
    delegation_input_id: str | None = None
    resource_refs: tuple[str, ...] = ()

    @classmethod
    def from_message(
        cls,
        message: MessageRecord,
        *,
        position: int | None = None,
        offset: int,
        resource_refs: tuple[str, ...] | None = None,
    ) -> FileMessageReference:
        delivery = message.metadata.get("context_provider_delivery_key")
        return cls(
            message_id=message.message_id,
            position=message.sequence if position is None else position,
            offset=offset,
            role=message.role,
            run_id=message.run_id,
            queued_input_id=message.input_id,
            assistant_turn_id=message.turn_id if message.role == "assistant" else None,
            context_delivery_key=delivery if isinstance(delivery, str) else None,
            call_keys=message_call_keys(message),
            input_origin=message_input_origin(message),
            delegation_input_id=delegation_input_id(message),
            resource_refs=tuple(message.resource_refs) if resource_refs is None else resource_refs,
        )


_SELECT = """SELECT message_id, position, offset, role, run_id, queued_input_id,
    assistant_turn_id, context_delivery_key, input_origin, delegation_input_id, resource_refs,
    (SELECT json_group_array(call_key) FROM call_memberships
     WHERE call_memberships.position = locations.position)
    FROM locations"""


def _reference(row: tuple) -> FileMessageReference:
    return FileMessageReference(
        message_id=row[0],
        position=row[1],
        offset=row[2],
        role=row[3],
        run_id=row[4],
        queued_input_id=row[5],
        assistant_turn_id=row[6],
        context_delivery_key=row[7],
        input_origin=row[8],
        delegation_input_id=row[9],
        resource_refs=tuple(json.loads(row[10])),
        call_keys=tuple(json.loads(row[11])),
    )


class FileMessageIndex:
    def create(
        self, connection: sqlite3.Connection, references: Sequence[FileMessageReference]
    ) -> None:
        counts: dict[str, RunMessageCounts] = {}
        calls: set[str] = set()
        for reference in references:
            if reference.run_id is None:
                continue
            if reference.run_id not in counts:
                counts[reference.run_id] = RunMessageCounts(run_id=reference.run_id)
            count = counts[reference.run_id]
            count.input_count += int(reference.input_origin is not None)
            count.automatic_input_count += int(reference.input_origin == "automatic")
            for key in reference.call_keys:
                if key not in calls:
                    count.tool_call_count += 1
                    calls.add(key)
        self._write(connection, references, tuple(counts.values()))

    @staticmethod
    def _write(
        connection: sqlite3.Connection,
        references: Sequence[FileMessageReference],
        counts: Sequence[RunMessageCounts],
    ) -> None:
        connection.executemany(
            "INSERT INTO locations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(position) DO UPDATE SET "
            "message_id = excluded.message_id, offset = excluded.offset, "
            "role = excluded.role, run_id = excluded.run_id, "
            "queued_input_id = excluded.queued_input_id, "
            "assistant_turn_id = excluded.assistant_turn_id, "
            "context_delivery_key = excluded.context_delivery_key, "
            "input_origin = excluded.input_origin, "
            "delegation_input_id = excluded.delegation_input_id, "
            "resource_refs = excluded.resource_refs",
            (
                (
                    item.position,
                    item.message_id,
                    item.offset,
                    item.role,
                    item.run_id,
                    item.queued_input_id,
                    item.assistant_turn_id,
                    item.context_delivery_key,
                    item.input_origin,
                    item.delegation_input_id,
                    json.dumps(item.resource_refs),
                )
                for item in references
            ),
        )
        connection.executemany(
            "INSERT OR IGNORE INTO call_memberships VALUES (?, ?)",
            ((key, item.position) for item in references for key in item.call_keys),
        )
        connection.executemany(
            "INSERT INTO counts VALUES (?, ?) ON CONFLICT(run_id) DO UPDATE SET "
            "payload = excluded.payload",
            ((item.run_id, item.model_dump_json()) for item in counts),
        )

    def apply(
        self,
        connection: sqlite3.Connection,
        references: Sequence[FileMessageReference],
        removed: Sequence[FileMessageReference],
        counts: Sequence[RunMessageCounts],
    ) -> None:
        if removed:
            cut = min(item.position for item in removed)
            connection.execute("DELETE FROM call_memberships WHERE position >= ?", (cut,))
            connection.execute("DELETE FROM locations WHERE position >= ?", (cut,))
        self._write(connection, references, counts)

    def get(self, connection: sqlite3.Connection, message_id: str) -> FileMessageReference | None:
        row = connection.execute(_SELECT + " WHERE message_id = ?", (message_id,)).fetchone()
        return _reference(row) if row is not None else None

    def contains_any(self, connection: sqlite3.Connection, identities: Sequence[str]) -> bool:
        if not identities:
            return False
        return (
            connection.execute(
                "SELECT 1 FROM locations "
                "WHERE message_id IN (SELECT value FROM json_each(?)) LIMIT 1",
                (json.dumps(identities),),
            ).fetchone()
            is not None
        )

    def selected(
        self, connection: sqlite3.Connection, kind: LookupKind, identity: str, *, last: bool = False
    ) -> list[FileMessageReference]:
        parameters: tuple[object, ...] = (identity,)
        if kind == "calls":
            where = "position IN (SELECT position FROM call_memberships WHERE call_key = ?)"
        elif kind == "input_messages":
            where = "role IN ('user', 'system')"
            parameters = ()
        elif kind in {"assistant_turns", "context_deliveries"}:
            field = "assistant_turn_id" if kind == "assistant_turns" else "context_delivery_key"
            where = f"run_id = ? AND {field} = ?"
            parameters = tuple(json.loads(identity))
        elif kind == "inputs":
            where = "queued_input_id = ?"
        else:
            where = "run_id = ?"
            if kind == "assistant_runs":
                where += " AND role = 'assistant'"
        order = " DESC LIMIT 1" if last else ""
        return [
            _reference(row)
            for row in connection.execute(
                _SELECT + f" WHERE {where} ORDER BY position{order}", parameters
            )
        ]

    def suffix(self, connection: sqlite3.Connection, message_id: str) -> list[FileMessageReference]:
        return [
            _reference(row)
            for row in connection.execute(
                _SELECT + " WHERE position >= "
                "(SELECT position - 1 FROM locations WHERE message_id = ?) "
                "ORDER BY position DESC",
                (message_id,),
            )
        ]

    def last_reference(
        self, connection: sqlite3.Connection, kind: LookupKind, identity: str
    ) -> FileMessageReference | None:
        selected = self.selected(connection, kind, identity, last=True)
        return selected[0] if selected else None

    @staticmethod
    def read_counts(connection: sqlite3.Connection, run_id: str) -> RunMessageCounts:
        row = connection.execute(
            "SELECT payload FROM counts WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is not None:
            counts = RunMessageCounts.model_validate_json(row[0])
            if counts.run_id != run_id:
                raise PersistenceError("Run message counts have a foreign identity")
            return counts
        if (
            connection.execute(
                "SELECT 1 FROM locations WHERE run_id = ? LIMIT 1",
                (run_id,),
            ).fetchone()
            is not None
        ):
            raise PersistenceError("published Run messages have no counts")
        return RunMessageCounts(run_id=run_id)

    def prepare_counts(
        self,
        connection: sqlite3.Connection,
        appended: Sequence[FileMessageReference],
        removed: Sequence[FileMessageReference],
    ) -> tuple[RunMessageCounts, ...]:
        if not appended and not removed:
            return ()
        counts: dict[str, RunMessageCounts] = {}

        def current(run_id: str) -> RunMessageCounts:
            if run_id not in counts:
                counts[run_id] = self.read_counts(connection, run_id)
            return counts[run_id]

        for change, references in ((-1, removed), (1, appended)):
            for reference in references:
                if reference.run_id is not None:
                    count = current(reference.run_id)
                    count.input_count += change * int(reference.input_origin is not None)
                    count.automatic_input_count += change * int(
                        reference.input_origin == "automatic"
                    )
        added_calls = {key for item in appended for key in item.call_keys}
        affected_calls = added_calls | {key for item in removed for key in item.call_keys}
        cut = min((item.position for item in removed), default=None)
        for key in affected_calls:
            before = (
                connection.execute(
                    "SELECT 1 FROM call_memberships WHERE call_key = ? LIMIT 1", (key,)
                ).fetchone()
                is not None
            )
            retained = (
                before
                if cut is None
                else connection.execute(
                    "SELECT 1 FROM call_memberships WHERE call_key = ? AND position < ? LIMIT 1",
                    (key, cut),
                ).fetchone()
                is not None
            )
            run_id, _, _ = json.loads(key)
            current(run_id).tool_call_count += int(retained or key in added_calls) - int(before)
        return tuple(RunMessageCounts.model_validate(vars(item)) for item in counts.values())
