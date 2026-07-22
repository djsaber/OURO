from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from pydantic import ValidationError

from ouro.core.agents import DelegationRecord
from ouro.core.base import new_id, utc_now
from ouro.core.compaction import CompactionRecord
from ouro.core.context_snapshots import ContextSnapshotRecord
from ouro.core.errors import MigrationRequiredError, PersistenceError, ResourceNotFoundError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.resources import SessionResourceReference
from ouro.core.state import RunRecord, SessionRecord
from ouro.local_locks import is_lock_held
from ouro.sessions.base import RuntimeSessionSnapshot, SessionSnapshot
from ouro.sessions.catalog import (
    catalog_entry_from_snapshot,
    sort_catalog_entries,
)
from ouro.sessions.index import SessionIndex
from ouro.sessions.locks import SessionRunLock

_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
SQLITE_SCHEMA_VERSION = 2


class SQLiteSessionStore:
    """SQLite implementation of OURO's canonical Session persistence contract."""

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database).expanduser().resolve()
        self._locks_dir = self.database.parent / f".{self.database.name}.locks"
        self._transaction_state = threading.local()

    def initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._locks_dir.mkdir(parents=True, exist_ok=True)
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS messages (
                        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        message_id TEXT NOT NULL UNIQUE,
                        sequence INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS messages_session_order
                        ON messages(session_id, row_id);
                    CREATE TABLE IF NOT EXISTS events (
                        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        event_id TEXT NOT NULL UNIQUE,
                        run_id TEXT,
                        sequence INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        UNIQUE(session_id, sequence),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS events_session_order
                        ON events(session_id, row_id);
                    CREATE TABLE IF NOT EXISTS runs (
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, run_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS compactions (
                        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        compaction_id TEXT NOT NULL UNIQUE,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS latest_contexts (
                        session_id TEXT PRIMARY KEY,
                        context_id TEXT NOT NULL UNIQUE,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS resource_references (
                        session_id TEXT NOT NULL,
                        resource_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, resource_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS resource_reference_identity
                        ON resource_references(resource_id);
                    CREATE TABLE IF NOT EXISTS trashed_sessions (
                        trash_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        trashed_at TEXT NOT NULL,
                        payload TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS delegations (
                        delegation_id TEXT PRIMARY KEY,
                        root_session_id TEXT NOT NULL,
                        parent_session_id TEXT NOT NULL,
                        child_session_id TEXT,
                        idempotency_key TEXT NOT NULL,
                        spawn_state TEXT NOT NULL,
                        delivery_status TEXT,
                        revision INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        UNIQUE(root_session_id, idempotency_key),
                        FOREIGN KEY(root_session_id)
                            REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS delegations_root_parent
                        ON delegations(root_session_id, parent_session_id, created_at);
                    CREATE UNIQUE INDEX IF NOT EXISTS delegations_child_session
                        ON delegations(child_session_id) WHERE child_session_id IS NOT NULL;
                    CREATE INDEX IF NOT EXISTS delegations_spawn_state
                        ON delegations(spawn_state);
                    CREATE INDEX IF NOT EXISTS delegations_delivery_status
                        ON delegations(delivery_status);
                """
            )
            existing = connection.execute(
                "SELECT value FROM metadata WHERE key = 'sqlite_schema_version'"
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('sqlite_schema_version', ?)",
                    (str(SQLITE_SCHEMA_VERSION),),
                )
            elif int(existing[0]) == 1:
                connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'sqlite_schema_version'",
                    (str(SQLITE_SCHEMA_VERSION),),
                )
            elif int(existing[0]) != SQLITE_SCHEMA_VERSION:
                raise MigrationRequiredError(
                    "OURO SQLite migration required: "
                    f"found {existing[0]}, expected {SQLITE_SCHEMA_VERSION}"
                )
            connection.commit()
        except MigrationRequiredError:
            if connection is not None:
                connection.rollback()
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            if connection is not None:
                connection.rollback()
            raise PersistenceError(
                f"could not initialize SQLite store {self.database}: {exc}"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    def create_session(self, session: SessionRecord) -> None:
        self.create_session_snapshot(session, [])

    def create_delegation(self, delegation: DelegationRecord) -> DelegationRecord:
        with self.session_mutation(delegation.root_session_id):
            connection = self._transaction_connection()
            self._require_session(connection, delegation.root_session_id)
            try:
                connection.execute(
                    "INSERT INTO delegations("
                    "delegation_id, root_session_id, parent_session_id, child_session_id, "
                    "idempotency_key, spawn_state, delivery_status, revision, created_at, payload"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    self._delegation_row(delegation),
                )
            except sqlite3.IntegrityError as exc:
                if self.find_delegation_by_idempotency_key(
                    delegation.root_session_id, delegation.idempotency_key
                ) is not None:
                    raise PersistenceError(
                        "delegation idempotency key already exists: "
                        f"{delegation.idempotency_key}"
                    ) from exc
                raise PersistenceError(
                    f"delegation already exists or is inconsistent: {delegation.delegation_id}"
                ) from exc
        return delegation

    def load_delegation(
        self,
        root_session_id: str,
        delegation_id: str,
    ) -> DelegationRecord:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM delegations "
                "WHERE root_session_id = ? AND delegation_id = ?",
                (root_session_id, delegation_id),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(delegation_id)
            return self._validate_model(
                DelegationRecord, row[0], f"delegation {delegation_id}"
            )

    def save_delegation(
        self,
        delegation: DelegationRecord,
        *,
        expected_revision: int,
    ) -> DelegationRecord:
        if delegation.revision != expected_revision:
            raise PersistenceError(
                "delegation revision conflict: supplied record does not match expected revision"
            )
        updated = DelegationRecord.model_validate(
            delegation.model_copy(
                update={"revision": expected_revision + 1, "updated_at": utc_now()}
            ).model_dump()
        )
        with self.session_mutation(delegation.root_session_id):
            connection = self._transaction_connection()
            cursor = connection.execute(
                "UPDATE delegations SET parent_session_id = ?, child_session_id = ?, "
                "spawn_state = ?, delivery_status = ?, revision = ?, payload = ? "
                "WHERE root_session_id = ? AND delegation_id = ? AND revision = ?",
                (
                    updated.parent_session_id,
                    updated.child_session_id,
                    updated.spawn_state,
                    self._delivery_status(updated),
                    updated.revision,
                    updated.model_dump_json(),
                    updated.root_session_id,
                    updated.delegation_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount == 0:
                exists = connection.execute(
                    "SELECT revision FROM delegations WHERE root_session_id = ? "
                    "AND delegation_id = ?",
                    (delegation.root_session_id, delegation.delegation_id),
                ).fetchone()
                if exists is None:
                    raise FileNotFoundError(delegation.delegation_id)
                raise PersistenceError(
                    "delegation revision conflict: "
                    f"expected {expected_revision}, found {exists[0]}"
                )
        return updated

    def list_delegations(
        self,
        root_session_id: str,
        *,
        parent_session_id: str | None = None,
    ) -> list[DelegationRecord]:
        query = "SELECT payload FROM delegations WHERE root_session_id = ?"
        parameters: list[object] = [root_session_id]
        if parent_session_id is not None:
            query += " AND parent_session_id = ?"
            parameters.append(parent_session_id)
        query += " ORDER BY created_at, delegation_id"
        with self._read_connection() as connection:
            self._require_session(connection, root_session_id)
            return self._load_models(
                connection, query, tuple(parameters), DelegationRecord
            )

    def find_delegation_by_child_session(
        self,
        child_session_id: str,
    ) -> DelegationRecord | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM delegations WHERE child_session_id = ?",
                (child_session_id,),
            ).fetchone()
            return (
                None
                if row is None
                else self._validate_model(
                    DelegationRecord, row[0], f"delegation for child {child_session_id}"
                )
            )

    def find_delegation_by_idempotency_key(
        self,
        root_session_id: str,
        idempotency_key: str,
    ) -> DelegationRecord | None:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM delegations "
                "WHERE root_session_id = ? AND idempotency_key = ?",
                (root_session_id, idempotency_key),
            ).fetchone()
            return (
                None
                if row is None
                else self._validate_model(
                    DelegationRecord, row[0], "delegation by idempotency key"
                )
            )

    def load_incomplete_delegations(self) -> list[DelegationRecord]:
        with self._read_connection() as connection:
            return self._load_models(
                connection,
                "SELECT payload FROM delegations "
                "WHERE spawn_state NOT IN ('submitted', 'failed') "
                "ORDER BY created_at, delegation_id",
                (),
                DelegationRecord,
            )

    def load_pending_delegation_deliveries(self) -> list[DelegationRecord]:
        with self._read_connection() as connection:
            return self._load_models(
                connection,
                "SELECT payload FROM delegations WHERE delivery_status = 'pending' "
                "ORDER BY created_at, delegation_id",
                (),
                DelegationRecord,
            )

    def create_session_snapshot(
        self,
        session: SessionRecord,
        messages: list[MessageRecord],
        resource_references: list[SessionResourceReference] | None = None,
    ) -> None:
        with self.session_mutation(session.session_id):
            connection = self._transaction_connection()
            try:
                connection.execute(
                    "INSERT INTO sessions(session_id, payload) VALUES(?, ?)",
                    (session.session_id, session.model_dump_json()),
                )
                for message in messages:
                    if message.session_id != session.session_id:
                        raise PersistenceError(
                            f"message {message.message_id} does not belong to {session.session_id}"
                        )
                    self._insert_message(connection, message)
                for reference in resource_references or ():
                    self._insert_resource_reference(connection, session.session_id, reference)
            except sqlite3.IntegrityError as exc:
                raise PersistenceError(f"session already exists: {session.session_id}") from exc

    def save_session(self, session: SessionRecord) -> None:
        with self.session_mutation(session.session_id):
            cursor = self._transaction_connection().execute(
                "UPDATE sessions SET payload = ? WHERE session_id = ?",
                (session.model_dump_json(), session.session_id),
            )
            if cursor.rowcount == 0:
                raise FileNotFoundError(session.session_id)

    def trash_session(self, session_id: str) -> str:
        with self.session_mutation(session_id):
            snapshot = self.load_session(session_id)
            references = self.load_resource_references(session_id)
            trash_id = new_id("trash")
            payload = json.dumps(
                {
                    "snapshot": snapshot.model_dump(mode="json"),
                    "resource_references": [
                        reference.model_dump(mode="json") for reference in references
                    ],
                },
                ensure_ascii=False,
            )
            connection = self._transaction_connection()
            connection.execute(
                "INSERT INTO trashed_sessions(trash_id, session_id, trashed_at, payload) "
                "VALUES(?, ?, ?, ?)",
                (trash_id, session_id, str(utc_now()), payload),
            )
            connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            return trash_id

    def append_message(self, message: MessageRecord) -> None:
        with self.session_mutation(message.session_id):
            try:
                self._insert_message(self._transaction_connection(), message)
            except sqlite3.IntegrityError as exc:
                raise PersistenceError(
                    f"could not append message {message.message_id}: {exc}"
                ) from exc

    def append_event(self, event: EventRecord) -> None:
        with self.session_mutation(event.session_id):
            try:
                self._insert_event(self._transaction_connection(), event)
            except sqlite3.IntegrityError as exc:
                raise PersistenceError(f"could not append event {event.event_id}: {exc}") from exc

    def append_ordered_event(self, event: EventRecord) -> EventRecord:
        with self.session_mutation(event.session_id):
            connection = self._transaction_connection()
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE session_id = ?",
                (event.session_id,),
            ).fetchone()
            ordered = event.model_copy(update={"sequence": int(row[0]) + 1})
            self._insert_event(connection, ordered)
            return ordered

    def save_run(self, run: RunRecord) -> None:
        with self.session_mutation(run.session_id):
            self._require_session(self._transaction_connection(), run.session_id)
            self._transaction_connection().execute(
                "INSERT INTO runs(session_id, run_id, started_at, payload) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(session_id, run_id) DO UPDATE SET "
                "started_at = excluded.started_at, payload = excluded.payload",
                (run.session_id, run.run_id, str(run.started_at), run.model_dump_json()),
            )

    def save_latest_context(self, context: ContextSnapshotRecord) -> None:
        with self.session_mutation(context.session_id):
            connection = self._transaction_connection()
            self._require_session(connection, context.session_id)
            connection.execute(
                "INSERT INTO latest_contexts(session_id, context_id, payload) VALUES(?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "context_id = excluded.context_id, payload = excluded.payload",
                (context.session_id, context.context_id, context.model_dump_json()),
            )

    def load_latest_context(self, session_id: str) -> ContextSnapshotRecord | None:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            row = connection.execute(
                "SELECT payload FROM latest_contexts WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return ContextSnapshotRecord.model_validate_json(row[0])
        except (ValidationError, ValueError) as exc:
            raise PersistenceError(
                f"could not load latest context for session {session_id}: {exc}"
            ) from exc

    def append_compaction(self, compaction: CompactionRecord) -> None:
        with self.session_mutation(compaction.session_id):
            self._require_session(self._transaction_connection(), compaction.session_id)
            try:
                self._transaction_connection().execute(
                    "INSERT INTO compactions(session_id, compaction_id, payload) VALUES(?, ?, ?)",
                    (
                        compaction.session_id,
                        compaction.compaction_id,
                        compaction.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PersistenceError(
                    f"compaction already exists: {compaction.compaction_id}"
                ) from exc

    def activate_compaction(self, session_id: str, compaction_id: str) -> SessionRecord:
        with self.session_mutation(session_id):
            connection = self._transaction_connection()
            row = connection.execute(
                "SELECT payload FROM compactions WHERE session_id = ? AND compaction_id = ?",
                (session_id, compaction_id),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(compaction_id)
            session = self.load_session_record(session_id)
            updated = session.model_copy(
                update={"active_compaction_id": compaction_id, "updated_at": utc_now()}
            )
            self.save_session(updated)
            return updated

    def load_session(self, session_id: str) -> SessionSnapshot:
        with self._read_connection() as connection:
            session = self._load_session_record(connection, session_id)
            return SessionSnapshot(
                session=session,
                messages=self._load_models(
                    connection,
                    "SELECT payload FROM messages WHERE session_id = ? ORDER BY row_id",
                    (session_id,),
                    MessageRecord,
                ),
                events=self._load_models(
                    connection,
                    "SELECT payload FROM events WHERE session_id = ? ORDER BY row_id",
                    (session_id,),
                    EventRecord,
                ),
                runs=self._load_runs(connection, session_id),
                compactions=self._load_models(
                    connection,
                    "SELECT payload FROM compactions WHERE session_id = ? ORDER BY row_id",
                    (session_id,),
                    CompactionRecord,
                ),
            )

    def load_runtime_session(self, session_id: str) -> RuntimeSessionSnapshot:
        with self._read_connection() as connection:
            session = self._load_session_record(connection, session_id)
            snapshot = RuntimeSessionSnapshot(
                session=session,
                messages=self._load_models(
                    connection,
                    "SELECT payload FROM messages WHERE session_id = ? ORDER BY row_id",
                    (session_id,),
                    MessageRecord,
                ),
                runs=self._load_runs(connection, session_id),
                compactions=self._load_models(
                    connection,
                    "SELECT payload FROM compactions WHERE session_id = ? ORDER BY row_id",
                    (session_id,),
                    CompactionRecord,
                ),
            )
            if session.active_compaction_id is not None and snapshot.active_compaction is None:
                raise PersistenceError(
                    f"session {session_id} references missing compaction "
                    f"{session.active_compaction_id}"
                )
            return snapshot

    def load_session_record(self, session_id: str) -> SessionRecord:
        with self._read_connection() as connection:
            return self._load_session_record(connection, session_id)

    def load_active_compaction(self, session_id: str) -> CompactionRecord | None:
        session = self.load_session_record(session_id)
        if session.active_compaction_id is None:
            return None
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM compactions WHERE session_id = ? AND compaction_id = ?",
                (session_id, session.active_compaction_id),
            ).fetchone()
            if row is None:
                raise PersistenceError(
                    f"session {session_id} references missing compaction "
                    f"{session.active_compaction_id}"
                )
            return CompactionRecord.model_validate_json(row[0])

    def latest_event_sequence(self, session_id: str) -> int:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return int(row[0])

    def load_messages_page(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        limit: int = 100,
    ) -> tuple[list[MessageRecord], int | None]:
        return self._load_page(
            session_id,
            table="messages",
            model_type=MessageRecord,
            cursor=cursor,
            limit=limit,
        )

    def load_events_page(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        limit: int = 100,
        run_id: str | None = None,
    ) -> tuple[list[EventRecord], int | None]:
        return self._load_page(
            session_id,
            table="events",
            model_type=EventRecord,
            cursor=cursor,
            limit=limit,
            run_id=run_id,
        )

    def load_events_after(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[list[EventRecord], bool]:
        after_sequence, limit = _validated_page_args(after_sequence, limit)
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            rows = connection.execute(
                "SELECT payload FROM events WHERE session_id = ? AND sequence > ? "
                "ORDER BY sequence LIMIT ?",
                (session_id, after_sequence, limit + 1),
            ).fetchall()
        return (
            [
                self._validate_model(EventRecord, row[0], "event after sequence")
                for row in rows[:limit]
            ],
            len(rows) > limit,
        )

    def load_run(self, session_id: str, run_id: str) -> RunRecord:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM runs WHERE session_id = ? AND run_id = ?",
                (session_id, run_id),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(run_id)
            return self._validate_model(RunRecord, row[0], f"run {run_id}")

    def load_runs(self, session_id: str) -> list[RunRecord]:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return self._load_runs(connection, session_id)

    def list_sessions(self) -> list[SessionRecord]:
        with self._read_connection() as connection:
            return self._load_models(
                connection,
                "SELECT payload FROM sessions ORDER BY session_id",
                (),
                SessionRecord,
            )

    def load_resource_references(self, session_id: str) -> list[SessionResourceReference]:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return self._load_models(
                connection,
                "SELECT payload FROM resource_references WHERE session_id = ? ORDER BY resource_id",
                (session_id,),
                SessionResourceReference,
            )

    def attach_resource_reference(
        self,
        session_id: str,
        reference: SessionResourceReference,
    ) -> None:
        with self.session_mutation(session_id):
            self._require_session(self._transaction_connection(), session_id)
            self._insert_resource_reference(
                self._transaction_connection(),
                session_id,
                reference,
            )

    def remove_resource_reference(self, session_id: str, resource_id: str) -> None:
        with self.session_mutation(session_id):
            cursor = self._transaction_connection().execute(
                "DELETE FROM resource_references WHERE session_id = ? AND resource_id = ?",
                (session_id, resource_id),
            )
            if cursor.rowcount == 0:
                raise ResourceNotFoundError(
                    f"resource {resource_id} is not attached to session {session_id}"
                )

    def is_resource_referenced(self, resource_id: str) -> bool:
        with self._read_connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM resource_references WHERE resource_id = ? LIMIT 1",
                    (resource_id,),
                ).fetchone()
                is not None
            )

    def refresh_catalog(
        self,
        session_id: str,
        snapshot: RuntimeSessionSnapshot | None = None,
    ) -> str | None:
        del session_id, snapshot
        return None

    def read_catalog(self) -> SessionIndex:
        return self._build_catalog()

    def acquire_session_lock(self, session_id: str, run_id: str) -> SessionRunLock | None:
        lock = SessionRunLock(
            self._lock_path(session_id),
            session_id=session_id,
            run_id=run_id,
        )
        try:
            return lock if lock.acquire() else None
        except OSError as exc:
            raise PersistenceError(
                f"could not acquire SQLite session lock for {session_id}: {exc}"
            ) from exc

    def session_mutation(self, session_id: str) -> AbstractContextManager[object, None]:
        _validated_record_id(session_id, kind="session")
        return _SQLiteMutation(self)

    def is_session_locked(self, session_id: str) -> bool:
        try:
            return is_lock_held(self._lock_path(session_id))
        except OSError as exc:
            raise PersistenceError(
                f"could not inspect SQLite session lock for {session_id}: {exc}"
            ) from exc

    def _build_catalog(self) -> SessionIndex:
        entries = []
        for session in self.list_sessions():
            snapshot = self.load_runtime_session(session.session_id)
            entries.append(
                catalog_entry_from_snapshot(
                    snapshot,
                    resource_count=len(self.load_resource_references(session.session_id)),
                )
            )
        return SessionIndex(sessions=sort_catalog_entries(entries))

    def _load_page(
        self,
        session_id: str,
        *,
        table: str,
        model_type: type[MessageRecord] | type[EventRecord],
        cursor: int,
        limit: int,
        run_id: str | None = None,
    ) -> tuple[list[Any], int | None]:
        cursor, limit = _validated_page_args(cursor, limit)
        if table not in {"messages", "events"}:
            raise ValueError(f"unsupported journal table: {table}")
        clauses = ["session_id = ?", "row_id > ?"]
        parameters: list[Any] = [session_id, cursor]
        if run_id is not None:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        parameters.append(limit + 1)
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            rows = connection.execute(
                f"SELECT row_id, payload FROM {table} "  # noqa: S608 - table is allow-listed.
                f"WHERE {' AND '.join(clauses)} ORDER BY row_id LIMIT ?",
                tuple(parameters),
            ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        records = [
            self._validate_model(model_type, row[1], f"{table} row {row[0]}") for row in selected
        ]
        return records, int(selected[-1][0]) if has_more and selected else None

    def _load_runs(self, connection: sqlite3.Connection, session_id: str) -> list[RunRecord]:
        return self._load_models(
            connection,
            "SELECT payload FROM runs WHERE session_id = ? ORDER BY started_at, run_id",
            (session_id,),
            RunRecord,
        )

    def _insert_message(self, connection: sqlite3.Connection, message: MessageRecord) -> None:
        self._require_session(connection, message.session_id)
        connection.execute(
            "INSERT INTO messages(session_id, message_id, sequence, payload) VALUES(?, ?, ?, ?)",
            (
                message.session_id,
                message.message_id,
                message.sequence,
                message.model_dump_json(),
            ),
        )

    def _insert_event(self, connection: sqlite3.Connection, event: EventRecord) -> None:
        self._require_session(connection, event.session_id)
        connection.execute(
            "INSERT INTO events(session_id, event_id, run_id, sequence, payload) "
            "VALUES(?, ?, ?, ?, ?)",
            (
                event.session_id,
                event.event_id,
                event.run_id,
                event.sequence,
                event.model_dump_json(),
            ),
        )

    @staticmethod
    def _insert_resource_reference(
        connection: sqlite3.Connection,
        session_id: str,
        reference: SessionResourceReference,
    ) -> None:
        connection.execute(
            "INSERT INTO resource_references(session_id, resource_id, payload) VALUES(?, ?, ?) "
            "ON CONFLICT(session_id, resource_id) DO NOTHING",
            (session_id, reference.resource_id, reference.model_dump_json()),
        )

    @staticmethod
    def _require_session(connection: sqlite3.Connection, session_id: str) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            is None
        ):
            raise FileNotFoundError(session_id)

    def _load_session_record(
        self,
        connection: sqlite3.Connection,
        session_id: str,
    ) -> SessionRecord:
        row = connection.execute(
            "SELECT payload FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(session_id)
        return self._validate_model(SessionRecord, row[0], f"session {session_id}")

    def _load_models(
        self,
        connection: sqlite3.Connection,
        query: str,
        parameters: tuple[Any, ...],
        model_type: type[Any],
    ) -> list[Any]:
        rows = connection.execute(query, parameters).fetchall()
        return [self._validate_model(model_type, row[0], model_type.__name__) for row in rows]

    @staticmethod
    def _validate_model(model_type: type[Any], payload: str, label: str) -> Any:
        try:
            return model_type.model_validate_json(payload)
        except (ValidationError, ValueError, TypeError) as exc:
            raise PersistenceError(f"could not load {label}: {exc}") from exc

    @classmethod
    def _delegation_row(cls, delegation: DelegationRecord) -> tuple[object, ...]:
        return (
            delegation.delegation_id,
            delegation.root_session_id,
            delegation.parent_session_id,
            delegation.child_session_id,
            delegation.idempotency_key,
            delegation.spawn_state,
            cls._delivery_status(delegation),
            delegation.revision,
            str(delegation.created_at),
            delegation.model_dump_json(),
        )

    @staticmethod
    def _delivery_status(delegation: DelegationRecord) -> str | None:
        return delegation.result.delivery.status if delegation.result is not None else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        state = getattr(self._transaction_state, "value", None)
        if state is not None:
            yield state.connection
            return
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            yield connection
        except sqlite3.Error as exc:
            raise PersistenceError(f"SQLite read failed: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()

    def _transaction_connection(self) -> sqlite3.Connection:
        state = getattr(self._transaction_state, "value", None)
        if state is None:
            raise RuntimeError("SQLite mutation requires an active transaction")
        return state.connection

    def _lock_path(self, session_id: str) -> Path:
        return self._locks_dir / f"{_validated_record_id(session_id, kind='session')}.lock"


class _TransactionState:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.depth = 1
        self.failed = False


class _SQLiteMutation(AbstractContextManager[object, None]):
    def __init__(self, store: SQLiteSessionStore) -> None:
        self.store = store
        self._outermost = False

    def __enter__(self) -> Self:
        state = getattr(self.store._transaction_state, "value", None)
        if state is not None:
            state.depth += 1
            return self
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            connection.close()
            raise PersistenceError(f"could not begin SQLite transaction: {exc}") from exc
        self.store._transaction_state.value = _TransactionState(connection)
        self._outermost = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc, traceback
        state: _TransactionState = self.store._transaction_state.value
        if exc_type is not None:
            state.failed = True
        state.depth -= 1
        if not self._outermost:
            return
        try:
            if state.failed:
                state.connection.rollback()
            else:
                state.connection.commit()
        except sqlite3.Error as error:
            state.connection.rollback()
            raise PersistenceError(f"SQLite transaction failed: {error}") from error
        finally:
            state.connection.close()
            del self.store._transaction_state.value


def _validated_record_id(value: str, *, kind: str) -> str:
    if not _RECORD_ID.fullmatch(value):
        raise ValueError(f"invalid {kind} id: {value!r}")
    return value


def _validated_page_args(cursor: int, limit: int) -> tuple[int, int]:
    if cursor < 0:
        raise ValueError("cursor must be non-negative")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    return cursor, limit
