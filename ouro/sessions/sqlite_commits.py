from __future__ import annotations

import sqlite3

from ouro.core.compaction import CompactionRecord
from ouro.core.errors import PersistenceError
from ouro.core.harness import HarnessVersion
from ouro.core.message_frames import MessageTruncation, retain_compactions
from ouro.core.messages import MessageRecord
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.session_commits import (
    HistoryBoundaryUnavailable,
    SessionCommit,
    SessionCommitConflict,
    SessionCommitResult,
    prepare_session_commit,
)
from ouro.core.state import SessionRecord


class SQLiteSessionCommits:
    """Native Session commits on the adapter's existing transaction connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def load_session(self, session_id: str) -> SessionRecord:
        row = self.connection.execute(
            "SELECT payload FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise FileNotFoundError(session_id)
        session = decode_versioned_record_json(SessionRecord, row[0], label="Session")
        if session.session_id != session_id:
            raise PersistenceError("Session record has a foreign identity")
        session.controls = self.load_harness(session_id, session.harness_version).state.controls
        return session

    def load_harness(self, session_id: str, version_id: str) -> HarnessVersion:
        row = self.connection.execute(
            "SELECT payload FROM harness_versions WHERE session_id = ? AND version_id = ?",
            (session_id, version_id),
        ).fetchone()
        if row is None:
            raise PersistenceError(f"Harness checkpoint not found: {version_id}")
        version = decode_versioned_record_json(HarnessVersion, row[0], label="Harness checkpoint")
        if version.session_id != session_id or version.version_id != version_id:
            raise PersistenceError("Harness checkpoint has a foreign identity")
        return version

    def save_version(self, version: HarnessVersion) -> None:
        row = self.connection.execute(
            "SELECT payload FROM harness_versions WHERE session_id = ? AND version_id = ?",
            (version.session_id, version.version_id),
        ).fetchone()
        if row is not None:
            if self.load_harness(version.session_id, version.version_id) != version:
                raise PersistenceError("immutable Harness checkpoint cannot be overwritten")
            return
        self.connection.execute(
            "INSERT INTO harness_versions(session_id, version_id, payload) VALUES(?, ?, ?)",
            (version.session_id, version.version_id, version.model_dump_json()),
        )

    def load_result(self, session_id: str, operation_id: str) -> SessionCommitResult | None:
        row = self.connection.execute(
            "SELECT payload FROM session_commits WHERE session_id = ? AND operation_id = ?",
            (session_id, operation_id),
        ).fetchone()
        if row is None:
            return None
        result = decode_versioned_record_json(SessionCommitResult, row[0], label="Session commit")
        if result.session.session_id != session_id or result.operation_id != operation_id:
            raise PersistenceError("Session commit result has a foreign identity")
        return result

    def save_result(self, result: SessionCommitResult) -> None:
        self.connection.execute(
            "INSERT INTO session_commits(session_id, operation_id, payload) VALUES(?, ?, ?)",
            (result.session.session_id, result.operation_id, result.model_dump_json()),
        )

    def commit(self, request: SessionCommit) -> SessionCommitResult:
        request = SessionCommit.model_validate(request.model_dump(mode="json"))
        session_id = request.before.session_id
        previous = self.load_result(session_id, request.operation_id)
        if previous is not None:
            if previous.request_digest != request.digest:
                raise SessionCommitConflict("operation identity was reused for different effects")
            return previous
        connection = self.connection
        current = self.load_session(session_id)
        harness = self.load_harness(session_id, current.harness_version)
        event_sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
        )
        truncation = None
        restore = None
        compactions = None
        cut_row = None
        if request.before_message_id is not None:
            row = connection.execute(
                "SELECT row_id, payload FROM messages WHERE session_id = ? AND message_id = ?",
                (session_id, request.before_message_id),
            ).fetchone()
            if row is None:
                raise HistoryBoundaryUnavailable("message boundary not found")
            cut_row = row[0]
            first = decode_versioned_record_json(MessageRecord, row[1], label="history boundary")
            if first.before_harness_version is None:
                raise HistoryBoundaryUnavailable("message has no Harness checkpoint")
            restore = self.load_harness(session_id, first.before_harness_version)
            previous_row = connection.execute(
                "SELECT payload FROM messages WHERE session_id = ? AND row_id < ? "
                "ORDER BY row_id DESC LIMIT 1",
                (session_id, cut_row),
            ).fetchone()
            retained = (
                None
                if previous_row is None
                else decode_versioned_record_json(
                    MessageRecord, previous_row[0], label="retained history head"
                )
            )
            removed = {
                row[0]
                for row in connection.execute(
                    "SELECT message_id FROM messages WHERE session_id = ? AND row_id >= ?",
                    (session_id, cut_row),
                )
            }
            truncation = MessageTruncation(
                first_removed=first, last_retained=retained, removed_count=len(removed)
            )
            records = [
                decode_versioned_record_json(CompactionRecord, row[0], label="compaction")
                for row in connection.execute(
                    "SELECT payload FROM compactions WHERE session_id = ? ORDER BY row_id",
                    (session_id,),
                )
            ]
            compactions = tuple(retain_compactions(records, removed))
        decision = prepare_session_commit(
            request,
            current,
            harness,
            event_sequence=event_sequence,
            truncation=truncation,
            restore=restore,
            compactions=compactions,
        )
        result = decision.result
        for version in decision.versions:
            self.save_version(version)
        if cut_row is not None:
            connection.execute(
                "DELETE FROM messages WHERE session_id = ? AND row_id >= ?", (session_id, cut_row)
            )
        for message in result.messages:
            connection.execute(
                "INSERT INTO messages(session_id, message_id, sequence, payload) "
                "VALUES(?, ?, ?, ?)",
                (session_id, message.message_id, message.sequence, message.model_dump_json()),
            )
        for run in decision.runs:
            connection.execute(
                "INSERT INTO runs(session_id, run_id, started_at, payload) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(session_id, run_id) DO UPDATE SET payload = excluded.payload",
                (session_id, run.run_id, str(run.started_at), run.model_dump_json()),
            )
        if decision.compactions is not None:
            retained_ids = {item.compaction_id for item in decision.compactions}
            for (identity,) in connection.execute(
                "SELECT compaction_id FROM compactions WHERE session_id = ?", (session_id,)
            ).fetchall():
                if identity not in retained_ids:
                    connection.execute(
                        "DELETE FROM compactions WHERE compaction_id = ?", (identity,)
                    )
        if decision.invalidate_context:
            connection.execute("DELETE FROM latest_contexts WHERE session_id = ?", (session_id,))
        connection.execute(
            "UPDATE sessions SET payload = ? WHERE session_id = ?",
            (result.session.model_dump_json(exclude={"controls"}), session_id),
        )
        for event in result.events:
            connection.execute(
                "INSERT INTO events(session_id, event_id, run_id, sequence, payload) "
                "VALUES(?, ?, ?, ?, ?)",
                (session_id, event.event_id, event.run_id, event.sequence, event.model_dump_json()),
            )
        self.save_result(result)
        return result
