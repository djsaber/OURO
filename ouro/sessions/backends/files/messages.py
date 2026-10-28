from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import BinaryIO, Literal

from ouro.core.errors import PersistenceError
from ouro.core.message_content import decode_message_content, materialize_owned_message
from ouro.core.message_statistics import (
    RunResultReferences,
    collect_run_result_references,
)
from ouro.core.messages import MessageFact, MessageRecord
from ouro.core.tool_facts import ToolCallRecord, ToolOutcomeRecord
from ouro.resources.files import read_resource_content
from ouro.resources.media import materialize_message_media
from ouro.sessions import sqlite_facts, sqlite_resources
from ouro.sessions.backends.files.inputs import read_input
from ouro.sessions.backends.files.message_locations import FileMessageIndex, FileMessageReference
from ouro.sessions.message_reads import resolve_messages


def _run_key(run_id: str, identity: str) -> str:
    return json.dumps((run_id, identity), ensure_ascii=False, separators=(",", ":"))


class FileMessageRecords:
    """Sequential canonical facts with small direct and secondary locations.

    The caller holds the Session read/mutation lock. Mutations are either in a
    private snapshot directory or replayed from the existing commit.json slot.
    No cold query builds a Session-wide in-memory offset index.
    """

    def __init__(self) -> None:
        self.index = FileMessageIndex()

    def create(
        self, connection: sqlite3.Connection, directory: Path, messages: Sequence[MessageRecord]
    ) -> None:
        references = []
        with (directory / "messages.jsonl").open("wb") as stream:
            for position, message in enumerate(messages, start=1):
                references.append(
                    FileMessageReference.from_message(
                        message, position=position, offset=stream.tell()
                    )
                )
                stream.write((message.storage_json() + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        self.index.create(connection, references)

    @staticmethod
    def entry(payload: str | bytes, *, session_id: str) -> MessageRecord:
        entry = decode_message_content(payload, label="message journal")
        if entry.session_id != session_id:
            raise PersistenceError("message journal contains a foreign Session")
        return entry

    def fact(
        self, connection: sqlite3.Connection, directory: Path, message_id: str
    ) -> MessageFact | None:
        reference = self.index.get(connection, message_id)
        if reference is None:
            return None
        return MessageFact.model_validate(
            vars(self.resolve_record(directory, reference, session_id=directory.name))
        )

    def message(
        self, connection: sqlite3.Connection, directory: Path, message_id: str
    ) -> MessageRecord | None:
        reference = self.index.get(connection, message_id)
        if reference is None:
            return None
        return self.resolve(connection, directory, reference, session_id=directory.name)

    @staticmethod
    def materialize(
        directory: Path,
        fact: MessageRecord,
        *,
        connection: sqlite3.Connection,
        include_media: bool = True,
    ) -> MessageRecord:
        reference = fact.outcome_ref
        message = materialize_owned_message(
            fact,
            read_input(directory, fact.session_id, fact.input_ref.input_id)
            if fact.input_ref is not None
            else None,
            calls=[
                call
                for identity in fact.referenced_call_ids
                if (
                    call := sqlite_facts.read_fact(
                        connection,
                        fact.session_id,
                        identity,
                        ToolCallRecord,
                        include_arguments=bool(fact.call_refs),
                    )
                )
                is not None
            ],
            outcome=sqlite_facts.read_fact(
                connection,
                fact.session_id,
                reference.identity,
                ToolOutcomeRecord,
                attempt=reference.outcome_attempt,
                include_structured=False,
            )
            if reference is not None and reference.outcome_attempt is not None
            else None,
        )
        return (
            materialize_message_media(
                message, lambda identity: FileMessageRecords.media(connection, directory, identity)
            )
            if include_media
            else message
        )

    @staticmethod
    def materialize_many(
        directory: Path,
        facts: Iterable[MessageRecord],
        *,
        connection: sqlite3.Connection,
        include_media: bool = True,
    ) -> list[MessageRecord]:
        messages = resolve_messages(
            facts,
            connection=connection,
            read_input=lambda session_id, input_id: read_input(directory, session_id, input_id),
        )
        return [
            materialize_message_media(
                message, lambda identity: FileMessageRecords.media(connection, directory, identity)
            )
            if include_media
            else message
            for message in messages
        ]

    @staticmethod
    def media(connection: sqlite3.Connection, directory: Path, resource_id: str) -> bytes:
        record = sqlite_resources.read_reference(connection, directory.name, resource_id)
        return read_resource_content(directory / "resources" / "files", record)

    def resolve(
        self,
        connection: sqlite3.Connection,
        directory: Path,
        reference: FileMessageReference,
        *,
        session_id: str,
        include_media: bool = True,
    ) -> MessageRecord:
        fact = self.resolve_record(directory, reference, session_id=session_id)
        return self.materialize(directory, fact, connection=connection, include_media=include_media)

    def resolve_record(
        self,
        directory: Path,
        reference: FileMessageReference,
        *,
        session_id: str,
        stream: BinaryIO | None = None,
    ) -> MessageRecord:
        if stream is None:
            with (directory / "messages.jsonl").open("rb") as opened:
                return self.resolve_record(
                    directory, reference, session_id=session_id, stream=opened
                )
        stream.seek(reference.offset)
        record = self.entry(stream.readline(), session_id=session_id)
        if record.message_id != reference.message_id:
            raise PersistenceError("message location references a foreign identity")
        return record

    def run(
        self, connection: sqlite3.Connection, directory: Path, run_id: str, *, session_id: str
    ) -> list[MessageRecord]:
        references = self.index.selected(connection, "runs", run_id)
        if any(reference.run_id != run_id for reference in references):
            raise PersistenceError("Run message lookup has a foreign identity")
        with (directory / "messages.jsonl").open("rb") as stream:
            return self.materialize_many(
                directory,
                (
                    self.resolve_record(directory, item, session_id=session_id, stream=stream)
                    for item in references
                ),
                connection=connection,
            )

    def result_references(
        self, connection: sqlite3.Connection, run_id: str, *, delegation_id: str
    ) -> RunResultReferences:
        references = self.index.selected(connection, "runs", run_id)
        return collect_run_result_references(
            (
                (item.message_id, item.role, item.delegation_input_id, item.resource_refs)
                for item in references
            ),
            delegation_id=delegation_id,
        )

    def queued(
        self, connection: sqlite3.Connection, directory: Path, queue_id: str, *, session_id: str
    ) -> MessageRecord | None:
        reference = self.index.last_reference(connection, "inputs", queue_id)
        if reference is None:
            return None
        if reference.queued_input_id != queue_id:
            raise PersistenceError("input message lookup has a foreign identity")
        return self.resolve(connection, directory, reference, session_id=session_id)

    def run_message(
        self,
        connection: sqlite3.Connection,
        directory: Path,
        *,
        session_id: str,
        run_id: str,
        kind: Literal["assistant_turns", "context_deliveries"],
        identity: str,
    ) -> MessageRecord | None:
        reference = self.index.last_reference(connection, kind, _run_key(run_id, identity))
        if reference is None:
            return None
        selected = (
            reference.assistant_turn_id
            if kind == "assistant_turns"
            else reference.context_delivery_key
        )
        if reference.run_id != run_id or selected != identity:
            raise PersistenceError("Run message lookup has a foreign identity")
        return self.resolve(connection, directory, reference, session_id=session_id)

    def page(
        self,
        connection: sqlite3.Connection,
        directory: Path,
        *,
        session_id: str,
        cursor: int,
        limit: int,
    ) -> tuple[list[MessageRecord], int | None]:
        path = directory / "messages.jsonl"
        try:
            size = path.stat().st_size
            if cursor > size:
                raise ValueError(f"cursor {cursor} is beyond end of journal ({size})")
            with path.open("rb") as stream:
                if cursor:
                    stream.seek(cursor - 1)
                    if stream.read(1) != b"\n":
                        raise ValueError(f"cursor {cursor} is not at a record boundary")
                stream.seek(cursor)
                records: list[MessageRecord] = []
                while len(records) < limit and (raw := stream.readline()):
                    if raw.strip():
                        records.append(self.entry(raw, session_id=session_id))
                position = stream.tell()
                records = self.materialize_many(directory, records, connection=connection)
            return records, position if position < size else None
        except (OSError, ValueError) as exc:
            raise PersistenceError(f"could not read message page from {path}: {exc}") from exc
