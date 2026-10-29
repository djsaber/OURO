from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Generator, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, closing, contextmanager, nullcontext
from datetime import datetime
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from pydantic import ValidationError

from ouro.core.agents import DelegationRecord
from ouro.core.base import VersionedOuroModel, new_id, utc_now
from ouro.core.compaction import CompactionRecord, validate_compaction_snapshot
from ouro.core.content import media_resource_ids
from ouro.core.context_selection import validate_media_selection
from ouro.core.context_snapshots import (
    ContextSnapshotRecord,
    ModelRequestHeader,
    RequestManifestRecord,
    validate_request_snapshot,
)
from ouro.core.errors import (
    DelegationRevisionConflictError,
    PersistenceError,
    UnsupportedDataVersionError,
)
from ouro.core.harness import HarnessState, HarnessVersion
from ouro.core.inputs import InputRecord, InputState, resolve_input_content, validate_input_snapshot
from ouro.core.message_content import (
    decode_message_content,
    materialize_owned_message,
    validate_tool_message_snapshot,
)
from ouro.core.message_frames import MessageTruncation
from ouro.core.message_statistics import RunMessageCounts, RunResultReferences
from ouro.core.messages import MessageFact, MessageRecord
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.provider import ProviderRetryNotice
from ouro.core.resources import ResourceRecord, SessionResourceReference
from ouro.core.response_drafts import ResponseDraftChunk, ResponseDraftPage
from ouro.core.session_commits import (
    SessionCommit,
    SessionCommitConflict,
    SessionCommitResult,
    SessionCommitStep,
)
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import RunRecord, RunRequestCounts, SessionRecord
from ouro.core.tool_batches import ToolCallState, validate_call_states
from ouro.core.tool_facts import (
    ToolCallFacts,
    ToolCallIdentity,
    ToolCallRecord,
    ToolCallStart,
    ToolOutcomeRecord,
)
from ouro.local_locks import FileMutationLock, is_lock_held
from ouro.resources.files import (
    read_resource_content,
    require_published_resource,
    session_resource_directory,
    verify_resource_files,
    write_resource_contents,
)
from ouro.resources.media import (
    capture_outcome_media,
    materialize_context_media,
    materialize_message_media,
    materialize_outcome_media,
    validate_media_references,
)
from ouro.resources.prepared import validate_prepared_resources
from ouro.sessions import sqlite_resources
from ouro.sessions.backends.sqlite.commits import SQLiteSessionCommits
from ouro.sessions.backends.sqlite.messages import read_counts as read_message_counts
from ouro.sessions.backends.sqlite.messages import read_result_references, write_messages
from ouro.sessions.base import (
    RuntimeSessionSnapshot,
    SessionHistorySnapshot,
    SessionSnapshot,
)
from ouro.sessions.catalog import (
    SessionCatalogProjection,
    catalog_entry_from_projection,
    catalog_entry_from_session,
    sort_catalog_entries,
)
from ouro.sessions.index import SessionIndex, SessionIndexEntry, invalid_session_index_entry
from ouro.sessions.locks import SessionRunLock
from ouro.sessions.message_reads import resolve_messages
from ouro.sessions.observation import FactCursor, SessionFactPage
from ouro.sessions.portable import SessionBundle, populate_sessions
from ouro.sessions.sqlite_compactions import insert_compaction
from ouro.sessions.sqlite_connections import ExecutionConnections, NativeSessionRunLock
from ouro.sessions.sqlite_drafts import append_chunk as append_draft_chunk
from ouro.sessions.sqlite_drafts import publish_draft
from ouro.sessions.sqlite_drafts import read_page as read_draft_page
from ouro.sessions.sqlite_facts import load_snapshot as load_tool_facts
from ouro.sessions.sqlite_facts import (
    load_states,
    read_call_state,
    read_fact,
    write_call_state,
    write_fact,
)
from ouro.sessions.sqlite_inputs import read_input, read_state, write_input, write_state
from ouro.sessions.sqlite_observation import (
    OBSERVATION_SCHEMA,
    advance_revision,
    read_changes,
    read_revision,
)
from ouro.sessions.sqlite_requests import read_latest_header, read_request_header, write_header
from ouro.sessions.sqlite_runs import (
    read_pending_runs,
    read_request_counts,
    read_run,
    write_run,
)
from ouro.sessions.sqlite_sessions import read_session

_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
SQLITE_SCHEMA_VERSION = 27


class SQLiteSessionStore:
    """SQLite implementation of OURO's canonical Session persistence contract."""

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database).expanduser().resolve()
        self._locks_dir = self.database.parent / f".{self.database.name}.locks"
        self._transaction_state = threading.local()
        self._read_state = threading.local()
        self._connections = ExecutionConnections(self.database)

    def import_bundle(
        self,
        bundle: SessionBundle,
        *,
        replace_session_ids: tuple[str, ...] = (),
        resource_contents: Mapping[str, Mapping[str, bytes]] | None = None,
    ) -> None:
        with ExitStack() as resource_locks:
            for identity in sorted(replace_session_ids):
                resource_locks.enter_context(self._resource_lock(identity))
            with self.session_mutation("data-import"):
                state: _TransactionState = self._transaction_state.value
                state.resource_locks.enter_context(resource_locks.pop_all())
                connection = self._transaction_connection()
                for identity in replace_session_ids:
                    self._retire_resource_directory(identity, new_id("replacement"))
                connection.executemany(
                    "DELETE FROM sessions WHERE session_id = ?",
                    ((identity,) for identity in replace_session_ids),
                )
                populate_sessions(self, bundle, resource_contents=resource_contents)
                for identity in sorted(
                    set(replace_session_ids)
                    | {item.snapshot.session.session_id for item in bundle.sessions}
                ):
                    self.refresh_catalog(identity)

    def initialize(self) -> None:
        lock = FileMutationLock(self._locks_dir / "initialize.lock")
        if not lock.acquire(timeout=5):
            raise PersistenceError(f"SQLite initialization lock timed out: {self.database}")
        try:
            self._initialize_schema()
        finally:
            lock.release()

    def _initialize_schema(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._locks_dir.mkdir(parents=True, exist_ok=True)
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            existing = None
            if tables:
                if "metadata" not in tables:
                    raise UnsupportedDataVersionError(
                        f"SQLite store has no format version: {self.database}"
                    )
                existing = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'sqlite_schema_version'"
                ).fetchone()
                if existing is None or existing[0] != str(SQLITE_SCHEMA_VERSION):
                    found = existing[0] if existing is not None else "missing"
                    raise UnsupportedDataVersionError(
                        f"unsupported OURO SQLite schema at {self.database}: "
                        f"found {found}, expected {SQLITE_SCHEMA_VERSION}; "
                        "use a new database or remove obsolete development data"
                    )
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS session_catalog (
                        session_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS messages (
                        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        message_id TEXT NOT NULL UNIQUE,
                        sequence INTEGER NOT NULL,
                        run_id TEXT,
                        input_origin TEXT,
                        role TEXT NOT NULL,
                        delegation_input_id TEXT,
                        resource_refs TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS inputs (
                        session_id TEXT NOT NULL,
                        input_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, input_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS run_message_counts (
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        tool_call_count INTEGER NOT NULL CHECK(tool_call_count >= 0),
                        input_count INTEGER NOT NULL CHECK(input_count >= 0),
                        automatic_input_count INTEGER NOT NULL
                            CHECK(automatic_input_count >= 0
                                  AND automatic_input_count <= input_count),
                        PRIMARY KEY(session_id, run_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS message_call_references (
                        message_row INTEGER NOT NULL,
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        call_key TEXT NOT NULL,
                        PRIMARY KEY(message_row, call_key),
                        FOREIGN KEY(message_row) REFERENCES messages(row_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS message_call_identity
                        ON message_call_references(session_id, call_key);
                    CREATE INDEX IF NOT EXISTS message_calls_suffix
                        ON message_call_references(session_id, message_row);
                    CREATE TABLE IF NOT EXISTS tool_calls (
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        batch_id TEXT NOT NULL,
                        tool_call_id TEXT NOT NULL,
                        projection TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, run_id, batch_id, tool_call_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS tool_call_starts (
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        batch_id TEXT NOT NULL,
                        tool_call_id TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, run_id, batch_id, tool_call_id, attempt),
                        FOREIGN KEY(session_id, run_id, batch_id, tool_call_id)
                            REFERENCES tool_calls(session_id, run_id, batch_id, tool_call_id)
                            ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS tool_outcomes (
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        batch_id TEXT NOT NULL,
                        tool_call_id TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        structured TEXT NOT NULL,
                        PRIMARY KEY(session_id, run_id, batch_id, tool_call_id, attempt),
                        FOREIGN KEY(session_id, run_id, batch_id, tool_call_id)
                            REFERENCES tool_calls(session_id, run_id, batch_id, tool_call_id)
                            ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS tool_call_states (
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        batch_id TEXT NOT NULL,
                        tool_call_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        modified_revision INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(session_id, run_id, batch_id, tool_call_id),
                        FOREIGN KEY(session_id, run_id, batch_id, tool_call_id)
                            REFERENCES tool_calls(session_id, run_id, batch_id, tool_call_id)
                            ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS input_states (
                        session_id TEXT NOT NULL,
                        input_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        modified_revision INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(session_id, input_id),
                        FOREIGN KEY(session_id, input_id) REFERENCES inputs(session_id, input_id)
                            ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS messages_run_result_references
                        ON messages(session_id, run_id, row_id, message_id, role,
                                    delegation_input_id, resource_refs);
                    CREATE INDEX IF NOT EXISTS messages_sequence ON messages(session_id, sequence);
                    CREATE INDEX IF NOT EXISTS messages_session_order
                        ON messages(session_id, row_id);
                    CREATE INDEX IF NOT EXISTS messages_session_run
                        ON messages(session_id, run_id, row_id);
                    CREATE INDEX IF NOT EXISTS messages_session_queued_input
                        ON messages(
                            session_id, json_extract(payload, '$.input_ref.input_id'));
                    CREATE TABLE IF NOT EXISTS runs (
                        session_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        history_epoch INTEGER NOT NULL,
                        needs_recovery INTEGER NOT NULL,
                        model_round_count INTEGER NOT NULL,
                        summary_request_count INTEGER NOT NULL,
                        payload TEXT NOT NULL,
                        modified_revision INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(session_id, run_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS runs_request_counts
                        ON runs(session_id, run_id, model_round_count, summary_request_count);
                    CREATE INDEX IF NOT EXISTS runs_pending
                        ON runs(session_id, history_epoch, started_at, run_id)
                        WHERE needs_recovery = 1;
                    CREATE TABLE IF NOT EXISTS compactions (
                        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        compaction_id TEXT NOT NULL UNIQUE,
                        previous_compaction_id TEXT,
                        covered_end_message_id TEXT NOT NULL,
                        history_boundary_message_id TEXT NOT NULL,
                        modified_revision INTEGER NOT NULL DEFAULT 0,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS compaction_references
                        ON compactions(session_id, row_id, compaction_id,
                            previous_compaction_id, covered_end_message_id,
                            history_boundary_message_id);
                    CREATE TABLE IF NOT EXISTS latest_contexts (
                        session_id TEXT PRIMARY KEY,
                        context_id TEXT NOT NULL UNIQUE,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS request_headers (
                        session_id TEXT NOT NULL,
                        context_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, context_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS response_streams (
                        session_id TEXT PRIMARY KEY,
                        context_id TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS response_drafts (
                        session_id TEXT NOT NULL,
                        position INTEGER NOT NULL,
                        channel TEXT NOT NULL,
                        content TEXT NOT NULL,
                        PRIMARY KEY(session_id, position),
                        FOREIGN KEY(session_id) REFERENCES response_streams(session_id)
                            ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS harness_versions (
                        session_id TEXT NOT NULL,
                        version_id TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(session_id, version_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                    );
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
                        parent_run_id TEXT NOT NULL,
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
                    CREATE INDEX IF NOT EXISTS delegations_root_run
                        ON delegations(root_session_id, parent_run_id, created_at, delegation_id);
                    CREATE UNIQUE INDEX IF NOT EXISTS delegations_child_session
                        ON delegations(child_session_id) WHERE child_session_id IS NOT NULL;
                    CREATE INDEX IF NOT EXISTS delegations_spawn_state
                        ON delegations(spawn_state);
                    CREATE INDEX IF NOT EXISTS delegations_delivery_status
                        ON delegations(delivery_status);
                    CREATE INDEX IF NOT EXISTS messages_assistant_turn
                        ON messages(session_id, run_id,
                            json_extract(payload, '$.turn_id'), row_id)
                        WHERE json_extract(payload, '$.role') = 'assistant';
                    CREATE INDEX IF NOT EXISTS messages_context_delivery
                        ON messages(session_id, run_id,
                            json_extract(payload, '$.metadata.context_provider_delivery_key'),
                            row_id)
                        WHERE json_extract(payload, '$.metadata.context_provider_delivery_key')
                            IS NOT NULL;
                    CREATE INDEX IF NOT EXISTS messages_assistant_run
                        ON messages(session_id, run_id, row_id)
                        WHERE json_extract(payload, '$.role') = 'assistant';
                    CREATE INDEX IF NOT EXISTS messages_input_history
                        ON messages(session_id, row_id)
                        WHERE json_extract(payload, '$.role') IN ('user', 'system');
                """
                + OBSERVATION_SCHEMA
                + sqlite_resources.RESOURCE_SCHEMA
            )
            if existing is None:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('sqlite_schema_version', ?)",
                    (str(SQLITE_SCHEMA_VERSION),),
                )
            connection.commit()
        except UnsupportedDataVersionError:
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
                self._connections.return_connection(connection)

    def create_session(self, session: SessionRecord) -> None:
        self.create_session_snapshot(session, [])

    def load_tool_call(self, session_id: str, identity: ToolCallIdentity) -> ToolCallRecord | None:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return read_fact(connection, session_id, identity, ToolCallRecord)

    def load_tool_call_start(
        self, session_id: str, identity: ToolCallIdentity, attempt: int
    ) -> ToolCallStart | None:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return read_fact(connection, session_id, identity, ToolCallStart, attempt=attempt)

    def load_tool_outcome(
        self, session_id: str, identity: ToolCallIdentity, attempt: int
    ) -> ToolOutcomeRecord | None:
        with self.session_read(session_id) as connection:
            self._require_session(connection, session_id)
            outcome = read_fact(
                connection, session_id, identity, ToolOutcomeRecord, attempt=attempt
            )
            return materialize_outcome_media(
                outcome, lambda resource_id: self._read_media(session_id, resource_id)
            )

    def _capture_tool_outcome(self, outcome: ToolOutcomeRecord) -> ToolOutcomeRecord:
        connection = self._transaction_connection()
        for identity in media_resource_ids(outcome.value.content_blocks):
            require_published_resource(
                session_resource_directory(self.database.parent, outcome.session_id),
                self.load_resource_reference(outcome.session_id, identity),
            )
        previous = read_fact(
            connection,
            outcome.session_id,
            outcome.identity,
            ToolOutcomeRecord,
            attempt=outcome.attempt,
        )
        if previous == outcome:
            return outcome

        def read(identity: str) -> bytes:
            return self._read_media(outcome.session_id, identity)

        def publish(record: SessionResourceReference, content: bytes) -> None:
            write_resource_contents(
                session_resource_directory(self.database.parent, outcome.session_id),
                ((record, content),),
            )
            self.attach_resource_reference(outcome.session_id, record)

        value = materialize_outcome_media(outcome, read)
        assert value is not None
        return capture_outcome_media(value, materialize_outcome_media(previous, read), publish)

    def tool_outcomes_use_resource(self, session_id: str, resource_id: str) -> bool:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return (
                connection.execute(
                    "SELECT 1 FROM tool_outcomes o WHERE session_id = ? AND ("
                    "EXISTS (SELECT 1 FROM json_each(o.payload, '$.value.resource_refs') r "
                    "WHERE r.value = ?) OR "
                    "EXISTS (SELECT 1 FROM json_each(o.payload, '$.value.content') c WHERE "
                    "CASE c.type WHEN 'object' THEN json_extract(c.value, '$.resource_id') "
                    "END = ?)) LIMIT 1",
                    (session_id, resource_id, resource_id),
                ).fetchone()
                is not None
            )

    def load_input(self, session_id: str, input_id: str) -> InputRecord | None:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return read_input(connection, session_id, input_id)

    def load_tool_call_state(
        self, session_id: str, identity: ToolCallIdentity
    ) -> ToolCallState | None:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return read_call_state(connection, session_id, identity)

    def load_input_state(self, session_id: str, input_id: str) -> InputState | None:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return read_state(connection, session_id, input_id)

    def create_delegation(self, delegation: DelegationRecord) -> DelegationRecord:
        with self.session_mutation(delegation.root_session_id):
            connection = self._transaction_connection()
            self._require_session(connection, delegation.root_session_id)
            try:
                connection.execute(
                    "INSERT INTO delegations("
                    "delegation_id, root_session_id, parent_session_id, parent_run_id, "
                    "child_session_id, "
                    "idempotency_key, spawn_state, delivery_status, revision, created_at, payload"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    self._delegation_row(delegation),
                )
            except sqlite3.IntegrityError as exc:
                if (
                    self.find_delegation_by_idempotency_key(
                        delegation.root_session_id, delegation.idempotency_key
                    )
                    is not None
                ):
                    raise PersistenceError(
                        f"delegation idempotency key already exists: {delegation.idempotency_key}"
                    ) from exc
                raise PersistenceError(
                    f"delegation already exists or is inconsistent: {delegation.delegation_id}"
                ) from exc
            advance_revision(connection, delegation.root_session_id)
        return delegation

    def load_delegation(
        self,
        root_session_id: str,
        delegation_id: str,
    ) -> DelegationRecord:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM delegations WHERE root_session_id = ? AND delegation_id = ?",
                (root_session_id, delegation_id),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(delegation_id)
            return self._validate_model(DelegationRecord, row[0], f"delegation {delegation_id}")

    def load_delegation_by_id(self, delegation_id: str) -> DelegationRecord:
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT payload FROM delegations WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(delegation_id)
            return self._validate_model(DelegationRecord, row[0], f"delegation {delegation_id}")

    def save_delegation(
        self,
        delegation: DelegationRecord,
        *,
        expected_revision: int,
    ) -> DelegationRecord:
        if delegation.revision != expected_revision:
            raise DelegationRevisionConflictError(
                "delegation revision conflict: supplied record does not match expected revision"
            )
        updated = DelegationRecord.model_validate(
            delegation.model_copy(
                update={"revision": expected_revision + 1, "updated_at": utc_now()}
            ).model_dump()
        )
        with self.session_mutation(delegation.root_session_id):
            current = self.load_delegation(delegation.root_session_id, delegation.delegation_id)
            if current.revision != expected_revision:
                raise DelegationRevisionConflictError(
                    "delegation revision conflict: "
                    f"expected {expected_revision}, found {current.revision}"
                )
            updated.require_frozen_contract(current)
            connection = self._transaction_connection()
            cursor = connection.execute(
                "UPDATE delegations SET parent_session_id = ?, parent_run_id = ?, "
                "child_session_id = ?, idempotency_key = ?, "
                "spawn_state = ?, delivery_status = ?, revision = ?, payload = ? "
                "WHERE root_session_id = ? AND delegation_id = ? AND revision = ?",
                (
                    updated.parent_session_id,
                    updated.parent_run_id,
                    updated.child_session_id,
                    updated.idempotency_key,
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
                raise DelegationRevisionConflictError(
                    f"delegation revision conflict: expected {expected_revision}"
                )
            advance_revision(connection, delegation.root_session_id)
        return updated

    def list_delegations(
        self,
        root_session_id: str,
        *,
        parent_session_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> list[DelegationRecord]:
        query = "SELECT payload FROM delegations WHERE root_session_id = ?"
        parameters: list[object] = [root_session_id]
        if parent_session_id is not None:
            query += " AND parent_session_id = ?"
            parameters.append(parent_session_id)
        if parent_run_id is not None:
            query += " AND parent_run_id = ?"
            parameters.append(parent_run_id)
        query += " ORDER BY created_at, delegation_id"
        with self._read_connection() as connection:
            self._require_session(connection, root_session_id)
            return self._load_models(connection, query, tuple(parameters), DelegationRecord)

    def find_delegation_by_child_session(
        self,
        root_session_id: str,
        child_session_id: str,
    ) -> DelegationRecord | None:
        with self._read_connection() as connection:
            self._require_session(connection, root_session_id)
            row = connection.execute(
                "SELECT payload FROM delegations "
                "WHERE root_session_id = ? AND child_session_id = ?",
                (root_session_id, child_session_id),
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
                "SELECT payload FROM delegations WHERE root_session_id = ? AND idempotency_key = ?",
                (root_session_id, idempotency_key),
            ).fetchone()
            return (
                None
                if row is None
                else self._validate_model(DelegationRecord, row[0], "delegation by idempotency key")
            )

    def create_session_snapshot(
        self,
        session: SessionRecord,
        messages: list[MessageRecord],
        resource_references: list[SessionResourceReference] | None = None,
        harness_versions: list[HarnessVersion] | None = None,
        compactions: list[CompactionRecord] | None = None,
        resource_contents: Iterable[tuple[ResourceRecord, bytes]] = (),
        *,
        prepared_resources: bool = False,
        inputs: tuple[InputRecord, ...] = (),
        input_states: tuple[InputState, ...] | None = None,
        tool_facts: ToolCallFacts | None = None,
        call_states: tuple[ToolCallState, ...] = (),
        request_headers: tuple[ModelRequestHeader, ...] = (),
        latest_manifest: RequestManifestRecord | None = None,
    ) -> None:
        messages = [
            item if item.sequence == index else item.model_copy(update={"sequence": index})
            for index, item in enumerate(messages, start=1)
        ]
        validate_request_snapshot(session.session_id, request_headers, latest_manifest)
        validate_media_references(
            messages,
            latest_manifest,
            (record.resource_id for record in resource_references or []),
            (tool_facts or ToolCallFacts()).outcomes,
        )
        compaction_records = compactions or []
        validate_compaction_snapshot(session, messages, compaction_records)
        if (boundary := session.context_selection.media_elided_through) is not None:
            validate_media_selection(
                session.context_selection,
                session.session_id,
                next((item for item in messages if item.message_id == boundary.message_id), None),
            )
        states = validate_input_snapshot(
            session.session_id, session.pending_input_ids, inputs, input_states, messages=messages
        )
        if tool_facts is not None:
            tool_facts.validate_snapshot(session.session_id)
        validate_call_states(
            call_states, tool_facts or ToolCallFacts(), session_id=session.session_id
        )
        validate_tool_message_snapshot(messages, tool_facts or ToolCallFacts())
        with self.session_mutation(session.session_id):
            if prepared_resources:
                validate_prepared_resources(
                    self.database.parent, session.session_id, resource_references or []
                )
            connection = self._transaction_connection()
            try:
                versions = (
                    harness_versions
                    if harness_versions is not None
                    else [
                        HarnessVersion(
                            session_id=session.session_id,
                            version_id=session.harness_version,
                            state=HarnessState(controls=session.controls),
                        )
                    ]
                )
                checkpoints = {version.version_id: version for version in versions}
                if session.harness_version not in checkpoints or any(
                    message.before_harness_version is not None
                    and message.before_harness_version not in checkpoints
                    for message in messages
                ):
                    raise PersistenceError("Session snapshot has a missing Harness checkpoint")
                connection.execute(
                    "INSERT INTO sessions(session_id, payload) VALUES(?, ?)",
                    (
                        session.session_id,
                        session.model_copy(
                            update={
                                "message_count": len(messages),
                                "controls": checkpoints[session.harness_version].state.controls,
                                "run_count": 0,
                                "resource_count": 0,
                                "last_message_id": messages[-1].message_id if messages else None,
                            }
                        ).model_dump_json(),
                    ),
                )
                storage = SQLiteSessionCommits(connection)
                for fact in tool_facts.records if tool_facts is not None else ():
                    write_fact(connection, fact)
                for call_state in call_states:
                    write_call_state(connection, call_state)
                for record in inputs:
                    write_input(connection, record)
                for state in states:
                    write_state(connection, state)
                for version in versions:
                    if version.session_id != session.session_id:
                        raise PersistenceError("Harness checkpoint belongs to another Session")
                    storage.save_version(version)
                for message in messages:
                    if message.session_id != session.session_id:
                        raise PersistenceError(
                            f"message {message.message_id} does not belong to {session.session_id}"
                        )
                write_messages(connection, messages)
                for compaction in compaction_records:
                    insert_compaction(connection, compaction)
                for header in request_headers:
                    connection.execute(
                        "INSERT INTO request_headers(session_id, context_id, payload) "
                        "VALUES(?, ?, ?)",
                        (session.session_id, header.context_id, header.model_dump_json()),
                    )
                if latest_manifest is not None:
                    connection.execute(
                        "INSERT INTO latest_contexts(session_id, context_id, payload) "
                        "VALUES(?, ?, ?)",
                        (
                            session.session_id,
                            latest_manifest.context_id,
                            latest_manifest.model_dump_json(),
                        ),
                    )
                    publish_draft(
                        connection,
                        next(
                            header
                            for header in request_headers
                            if header.context_id == latest_manifest.context_id
                        ),
                    )
                write_resource_contents(
                    session_resource_directory(self.database.parent, session.session_id),
                    resource_contents,
                )
                if not prepared_resources:
                    verify_resource_files(
                        session_resource_directory(self.database.parent, session.session_id),
                        resource_references or (),
                    )
                for reference in resource_references or ():
                    sqlite_resources.attach_reference(connection, session.session_id, reference)
            except sqlite3.IntegrityError as exc:
                raise PersistenceError(f"session already exists: {session.session_id}") from exc

    def save_session(self, session: SessionRecord) -> None:
        with self.session_mutation(session.session_id):
            current = self.load_session_record(session.session_id)
            if session.history_epoch != current.history_epoch:
                raise SessionCommitConflict("Session history changed; stale Session write rejected")
            state = self.load_harness(session.session_id).state
            steps: tuple[SessionCommitStep, ...] = ()
            if (
                session.harness_version == current.harness_version
                and session.controls != current.controls
            ):
                steps = (
                    SessionCommitStep(
                        harness=state.model_copy(update={"controls": session.controls})
                    ),
                )
            self.commit_session(SessionCommit(before=current, after=session, steps=steps))

    def trash_session_tree(
        self,
        root_session_id: str,
        session_ids: tuple[str, ...],
    ) -> str:
        _validated_record_id(root_session_id, kind="session")
        normalized = tuple(dict.fromkeys(session_ids))
        if not normalized:
            raise ValueError("session tree cannot be empty")
        for session_id in normalized:
            _validated_record_id(session_id, kind="session")
        with ExitStack() as resource_locks:
            for identity in sorted(normalized):
                resource_locks.enter_context(self._resource_lock(identity))
            with self.session_mutation(root_session_id):
                state: _TransactionState = self._transaction_state.value
                state.resource_locks.enter_context(resource_locks.pop_all())
                connection = self._transaction_connection()
                for session_id in normalized:
                    self._require_session(connection, session_id)
                trash_id = new_id("trash")
                payload = json.dumps(
                    {
                        "schema_version": 1,
                        "root_session_id": root_session_id,
                        "session_ids": list(normalized),
                        "records": {
                            table: self._raw_trash_tree_rows(
                                connection,
                                table,
                                session_ids=(
                                    (root_session_id,) if table == "delegations" else normalized
                                ),
                                key=("root_session_id" if table == "delegations" else "session_id"),
                            )
                            for table in (
                                "sessions",
                                "messages",
                                "runs",
                                "compactions",
                                "latest_contexts",
                                "request_headers",
                                "resource_references",
                                "harness_versions",
                                "inputs",
                                "input_states",
                                "tool_calls",
                                "tool_call_starts",
                                "tool_outcomes",
                                "tool_call_states",
                                "delegations",
                            )
                        },
                    },
                    ensure_ascii=False,
                )
                connection.execute(
                    "INSERT INTO trashed_sessions(trash_id, session_id, trashed_at, payload) "
                    "VALUES(?, ?, ?, ?)",
                    (trash_id, root_session_id, str(utc_now()), payload),
                )
                placeholders = ", ".join("?" for _ in normalized)
                connection.execute(
                    f"DELETE FROM sessions WHERE session_id IN ({placeholders})",  # noqa: S608
                    normalized,
                )
                for session_id in normalized:
                    self._retire_resource_directory(session_id, trash_id)
                return trash_id

    def append_message(self, message: MessageRecord) -> None:
        self.append_messages([message])

    def append_messages(self, messages: list[MessageRecord]) -> None:
        if not messages:
            return
        with self.session_mutation(messages[0].session_id):
            current = self.load_session_record(messages[0].session_id)
            result = self.commit_session(
                SessionCommit(
                    before=current,
                    steps=tuple(SessionCommitStep(message=item) for item in messages),
                )
            )
            for supplied, stored in zip(messages, result.messages, strict=True):
                supplied.sequence = stored.sequence
                supplied.before_harness_version = stored.before_harness_version

    def load_messages_tail(
        self, session_id: str, *, limit: int = 100, include_media: bool = True
    ) -> list[MessageRecord]:
        if limit < 1:
            raise ValueError("message tail limit must be positive")
        with self.session_read(session_id) as connection:
            self._require_session(connection, session_id)
            rows = connection.execute(
                "SELECT payload FROM messages WHERE session_id = ? ORDER BY row_id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            return self._materialize_messages(
                connection, (row[0] for row in rows), "message tail", include_media=include_media
            )

    def iter_messages_before(
        self, session_id: str, *, before: int | None = None
    ) -> Generator[tuple[int, MessageRecord]]:
        if before is not None and before < 0:
            raise ValueError("message cursor must be nonnegative")
        with self.session_read(session_id) as connection:
            self._require_session(connection, session_id)
            query = "SELECT row_id, payload FROM messages WHERE session_id = ?"
            parameters: tuple[object, ...] = (session_id,)
            if before is not None:
                query += " AND row_id < ?"
                parameters += (before,)
            with closing(connection.execute(query + " ORDER BY row_id DESC", parameters)) as rows:
                for row in rows:
                    yield row[0], self._materialize_message(connection, row[1], "message history")

    def truncate_messages(
        self, session_id: str, *, before_message_id: str, expected_last_message_id: str
    ) -> MessageTruncation:
        with self.session_mutation(session_id):
            current = self.load_session_record(session_id)
            if current.last_message_id != expected_last_message_id:
                raise SessionCommitConflict("session history changed; reload before truncating")
            result = self.commit_session(
                SessionCommit(before=current, before_message_id=before_message_id)
            )
            assert result.truncation is not None
            return result.truncation

    def load_harness(self, session_id: str, version_id: str | None = None) -> HarnessVersion:
        with self._read_connection() as connection:
            commits = SQLiteSessionCommits(connection)
            if version_id is None:
                return commits.load_session_harness(session_id)[1]
            return commits.load_harness(session_id, version_id)

    def commit_session(self, request: SessionCommit) -> SessionCommitResult:
        with self.session_mutation(request.before.session_id):
            try:
                if request.tool_facts.outcomes:
                    request = request.model_copy(
                        update={
                            "tool_facts": request.tool_facts.model_copy(
                                update={
                                    "outcomes": tuple(
                                        self._capture_tool_outcome(outcome)
                                        for outcome in request.tool_facts.outcomes
                                    )
                                }
                            )
                        }
                    )
                for identity in request.media_resource_ids:
                    require_published_resource(
                        session_resource_directory(self.database.parent, request.before.session_id),
                        self.load_resource_reference(request.before.session_id, identity),
                    )
                return SQLiteSessionCommits(self._transaction_connection()).commit(request)
            except sqlite3.IntegrityError as exc:
                raise SessionCommitConflict(
                    f"Session commit violates record identity: {exc}"
                ) from exc

    def commit_tool_outcome(self, outcome: ToolOutcomeRecord) -> ToolOutcomeRecord:
        with self.session_mutation(outcome.session_id):
            connection = self._transaction_connection()
            self._require_session(connection, outcome.session_id)
            try:
                outcome = self._capture_tool_outcome(outcome)
                saved = SQLiteSessionCommits(connection).commit_outcome(outcome)
                value = materialize_outcome_media(
                    saved, lambda identity: self._read_media(outcome.session_id, identity)
                )
                assert value is not None
                return value
            except sqlite3.Error as exc:
                raise PersistenceError(f"could not save tool Outcome: {exc}") from exc

    def save_run(self, run: RunRecord) -> None:
        with self.session_mutation(run.session_id):
            revision = advance_revision(self._transaction_connection(), run.session_id)
            added = write_run(
                self._transaction_connection(),
                run,
                history_epoch=self.load_session_record(run.session_id).history_epoch,
                revision=revision,
            )
            if added:
                self._transaction_connection().execute(
                    "UPDATE sessions SET payload = json_set(payload, '$.run_count', "
                    "json_extract(payload, '$.run_count') + 1) WHERE session_id = ?",
                    (run.session_id,),
                )

    def append_response_draft(
        self, session_id: str, context_id: str, chunk: ResponseDraftChunk
    ) -> int:
        with _SQLiteMutation(self, durable=False):
            return append_draft_chunk(self._transaction_connection(), session_id, context_id, chunk)

    def load_response_draft(
        self, session_id: str, context_id: str, *, after_cursor: int = 0, limit: int | None = None
    ) -> ResponseDraftPage | None:
        if after_cursor < 0 or (limit is not None and limit < 1):
            raise ValueError("Draft cursor and page size must be positive")
        with self.session_read(session_id) as connection:
            page = read_draft_page(
                connection, session_id, context_id, after_cursor=after_cursor, limit=limit
            )
            if page is not None:
                return page
            header = self.load_latest_context_header(session_id)
            if (
                header is None
                or header.context_id != context_id
                or header.response_closed_at is not None
            ):
                return None
            return ResponseDraftPage(context_id=context_id, cursor=after_cursor)

    def record_request_retry(
        self, session_id: str, context_id: str, retry: ProviderRetryNotice
    ) -> None:
        with self.session_mutation(session_id):
            header = self.load_request_header(session_id, context_id)
            if header is None:
                raise PersistenceError(f"request not found: {context_id}")
            connection = self._transaction_connection()
            write_header(connection, header.model_copy(update={"retry": retry}))
            advance_revision(connection, session_id)

    def settle_request_header(
        self,
        session_id: str,
        context_id: str,
        *,
        settled_at: datetime,
        provider_done: bool,
        usage_entry_id: str | None,
    ) -> ModelRequestHeader:
        with self.session_mutation(session_id):
            header = self.load_request_header(session_id, context_id)
            if header is None:
                raise PersistenceError(f"latest context does not match {context_id}")
            completed = header.settled(
                context_id=context_id,
                settled_at=settled_at,
                provider_done=provider_done,
                usage_entry_id=usage_entry_id,
            )
            if completed != header:
                write_header(self._transaction_connection(), completed)
                advance_revision(self._transaction_connection(), session_id)
            return completed

    def load_latest_context(self, session_id: str) -> ContextSnapshotRecord | None:
        with self.session_read(session_id):
            request = self.load_latest_request(session_id)
            if request is None:
                return None
            header, body = request
            context = ContextSnapshotRecord.assemble(
                header,
                body,
                {
                    identity: self.load_message(session_id, identity)
                    for identity in body.manifest.referenced_message_ids
                },
            )
            return materialize_context_media(
                context, lambda identity: self._read_media(session_id, identity)
            )

    def load_latest_request(
        self, session_id: str
    ) -> tuple[ModelRequestHeader, RequestManifestRecord] | None:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            row = connection.execute(
                "SELECT h.payload, b.payload FROM latest_contexts b "
                "LEFT JOIN request_headers h USING(session_id, context_id) WHERE b.session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            if row[0] is None:
                raise PersistenceError("context input references missing header")
            body = decode_versioned_record_json(
                RequestManifestRecord, row[1], label="context input"
            )
            header = decode_versioned_record_json(
                ModelRequestHeader, row[0], label="context header"
            )
            if header.context_id != body.context_id or header.session_id != session_id:
                raise PersistenceError("context header and input have different identities")
            return header, body

    def load_latest_context_header(self, session_id: str) -> ModelRequestHeader | None:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return read_latest_header(connection, session_id)

    def load_request_header(self, session_id: str, context_id: str) -> ModelRequestHeader | None:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return read_request_header(connection, session_id, context_id)

    def load_session(self, session_id: str) -> SessionSnapshot:
        with self.session_read(session_id) as connection:
            session = read_session(connection, session_id)
            manifests = self._load_models(
                connection,
                "SELECT payload FROM latest_contexts WHERE session_id = ?",
                (session_id,),
                RequestManifestRecord,
            )
            # Records are decoded at their storage boundary; assembling a typed
            # container must not validate the entire history a second time.
            snapshot = SessionSnapshot.model_construct(
                session=session,
                request_headers=self._load_models(
                    connection,
                    "SELECT payload FROM request_headers WHERE session_id = ? ORDER BY context_id",
                    (session_id,),
                    ModelRequestHeader,
                ),
                latest_manifest=manifests[0] if manifests else None,
                tool_facts=load_tool_facts(connection, session_id),
                call_states=load_states(connection, session_id),
                inputs=self._load_models(
                    connection,
                    "SELECT payload FROM inputs WHERE session_id = ? ORDER BY input_id",
                    (session_id,),
                    InputRecord,
                ),
                input_states=self._load_models(
                    connection,
                    "SELECT payload FROM input_states WHERE session_id = ? ORDER BY input_id",
                    (session_id,),
                    InputState,
                ),
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
                harness_versions=self._load_models(
                    connection,
                    "SELECT payload FROM harness_versions WHERE session_id = ? ORDER BY version_id",
                    (session_id,),
                    HarnessVersion,
                ),
                extension_states=list(
                    SQLiteSessionCommits(connection)
                    .load_harness(session_id, session.harness_version)
                    .state.states
                ),
            )

            validate_request_snapshot(
                session_id, snapshot.request_headers, snapshot.latest_manifest
            )
            validate_call_states(snapshot.call_states, snapshot.tool_facts, session_id=session_id)
            return snapshot

    def load_runtime_session(self, session_id: str) -> RuntimeSessionSnapshot:
        with self._read_connection() as connection:
            session = read_session(connection, session_id)
            selected = {
                run.run_id: run
                for run in read_pending_runs(connection, session_id, session.history_epoch)
            }
            for identity in (session.active_run_id, session.last_run_id):
                if identity is not None and identity not in selected:
                    try:
                        selected[identity] = read_run(connection, session_id, identity)
                    except FileNotFoundError:
                        pass
            return RuntimeSessionSnapshot.model_construct(
                session=session,
                runs=sorted(selected.values(), key=lambda run: (str(run.started_at), run.run_id)),
            )

    def load_pending_runs(self, session_id: str) -> list[RunRecord]:
        with self._read_connection() as connection:
            session = read_session(connection, session_id)
            return read_pending_runs(connection, session_id, session.history_epoch)

    def load_session_observation(self, session_id: str) -> SessionHistorySnapshot:
        with self._read_connection() as connection:
            session = read_session(connection, session_id)
            snapshot = SessionHistorySnapshot.model_construct(
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
            if (
                session.context_selection.compaction_id is not None
                and snapshot.active_compaction is None
            ):
                raise PersistenceError(
                    f"session {session_id} references missing compaction "
                    f"{session.context_selection.compaction_id}"
                )
            return snapshot

    def load_session_record(self, session_id: str) -> SessionRecord:
        with self._read_connection() as connection:
            return read_session(connection, session_id)

    def load_session_harness(self, session_id: str) -> tuple[SessionRecord, HarnessVersion]:
        with self._read_connection() as connection:
            return SQLiteSessionCommits(connection).load_session_harness(session_id)

    def load_active_compaction(self, session_id: str) -> CompactionRecord | None:
        with self.session_read(session_id):
            identity = self.load_session_record(session_id).context_selection.compaction_id
            return self.load_compaction(session_id, identity) if identity is not None else None

    def load_compaction(self, session_id: str, compaction_id: str) -> CompactionRecord:
        with self.session_read(session_id) as connection:
            row = connection.execute(
                "SELECT payload FROM compactions WHERE session_id = ? AND compaction_id = ?",
                (session_id, compaction_id),
            ).fetchone()
            if row is None:
                raise PersistenceError(
                    f"session {session_id} references missing compaction {compaction_id}"
                )
            return self._validate_model(CompactionRecord, row[0], f"compaction {compaction_id}")

    def load_compactions(self, session_id: str) -> list[CompactionRecord]:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return self._load_models(
                connection,
                "SELECT payload FROM compactions WHERE session_id = ? ORDER BY row_id",
                (session_id,),
                CompactionRecord,
            )

    def load_compactions_in_range(
        self, session_id: str, *, first_sequence: int, last_sequence: int
    ) -> list[CompactionRecord]:
        with self.session_read(session_id) as connection:
            rows = connection.execute(
                "SELECT c.payload FROM messages AS m CROSS JOIN compactions AS c "
                "ON c.session_id = m.session_id AND c.history_boundary_message_id = m.message_id "
                "WHERE m.session_id = ? AND m.sequence BETWEEN ? AND ? ORDER BY c.row_id",
                (session_id, first_sequence, last_sequence),
            )
            return [
                decode_versioned_record_json(CompactionRecord, row[0], label="compaction")
                for row in rows
            ]

    def load_observation_changes(
        self,
        session_id: str,
        *,
        after_revision: int,
        through_revision: int,
        limit: int,
        cursor: FactCursor | None = None,
    ) -> SessionFactPage:
        with self._read_connection() as connection:
            return read_changes(
                connection,
                session_id,
                after_revision=after_revision,
                through_revision=through_revision,
                limit=limit,
                cursor=cursor,
            )

    def observation_revision(self, session_id: str) -> int:
        with self._read_connection() as connection:
            return read_revision(connection, session_id)

    def load_messages_after(
        self,
        session_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> tuple[list[MessageRecord], bool]:
        with self.session_read(session_id) as connection:
            rows = connection.execute(
                "SELECT payload FROM messages WHERE session_id = ? "
                "AND sequence > ? ORDER BY sequence LIMIT ?",
                (session_id, after_sequence, limit + 1),
            ).fetchall()
            return self._materialize_messages(
                connection, (row[0] for row in rows[:limit]), "observed messages"
            ), len(rows) > limit

    def load_messages_page(
        self,
        session_id: str,
        *,
        cursor: int = 0,
        limit: int = 100,
    ) -> tuple[list[MessageRecord], int | None]:
        cursor, limit = _validated_page_args(cursor, limit)
        with self.session_read(session_id) as connection:
            self._require_session(connection, session_id)
            rows = connection.execute(
                "SELECT row_id, payload FROM messages WHERE session_id = ? "
                "AND row_id > ? ORDER BY row_id LIMIT ?",
                (session_id, cursor, limit + 1),
            ).fetchall()
            records = self._materialize_messages(
                connection, (row[1] for row in rows[:limit]), "messages"
            )
        return records, int(rows[limit - 1][0]) if len(rows) > limit else None

    def load_message(self, session_id: str, message_id: str) -> MessageRecord:
        with self.session_read(session_id) as connection:
            self._require_session(connection, session_id)
            row = connection.execute(
                "SELECT payload FROM messages WHERE session_id = ? AND message_id = ?",
                (session_id, message_id),
            ).fetchone()
            if row is None:
                raise PersistenceError(f"message not found in session {session_id}: {message_id}")
            return self._materialize_message(connection, row[0], f"message {message_id}")

    def load_message_fact(self, session_id: str, message_id: str) -> MessageFact:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            row = connection.execute(
                "SELECT payload FROM messages WHERE session_id = ? AND message_id = ?",
                (session_id, message_id),
            ).fetchone()
            if row is None:
                raise PersistenceError(f"message not found in session {session_id}: {message_id}")
            fact = decode_versioned_record_json(MessageFact, row[0], label=f"message {message_id}")
            if fact.session_id != session_id or fact.message_id != message_id:
                raise PersistenceError("message record belongs to another Session")
            return fact

    def load_run_message_counts(self, session_id: str, run_id: str) -> RunMessageCounts:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return read_message_counts(connection, session_id, run_id)

    def load_run_result_references(
        self, session_id: str, run_id: str, *, delegation_id: str
    ) -> RunResultReferences:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return read_result_references(
                connection, session_id, run_id, delegation_id=delegation_id
            )

    def load_run_messages(self, session_id: str, run_id: str) -> list[MessageRecord]:
        with self.session_read(session_id) as connection:
            self._require_session(connection, session_id)
            return self._load_models(
                connection,
                "SELECT payload FROM messages WHERE session_id = ? AND run_id = ? ORDER BY row_id",
                (session_id, run_id),
                MessageRecord,
            )

    def run_message_end(self, session_id: str, run_id: str) -> int:
        with self.session_read(session_id) as connection:
            row = connection.execute(
                "SELECT sequence FROM messages WHERE session_id = ? AND run_id = ? "
                "ORDER BY row_id DESC LIMIT 1",
                (session_id, run_id),
            ).fetchone()
            return row[0] if row is not None else 0

    def has_run_messages(self, session_id: str, run_id: str) -> bool:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return (
                connection.execute(
                    "SELECT 1 FROM messages WHERE session_id = ? AND run_id = ? LIMIT 1",
                    (session_id, run_id),
                ).fetchone()
                is not None
            )

    def has_input_messages(self, session_id: str) -> bool:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return (
                connection.execute(
                    "SELECT 1 FROM messages WHERE session_id = ? "
                    "AND json_extract(payload, '$.role') IN ('user', 'system') LIMIT 1",
                    (session_id,),
                ).fetchone()
                is not None
            )

    def find_last_assistant_message(self, session_id: str, run_id: str) -> MessageRecord | None:
        with self.session_read(session_id) as connection:
            self._require_session(connection, session_id)
            row = connection.execute(
                "SELECT payload FROM messages WHERE session_id = ? "
                "AND run_id = ? "
                "AND json_extract(payload, '$.role') = 'assistant' ORDER BY row_id DESC LIMIT 1",
                (session_id, run_id),
            ).fetchone()
            return (
                self._materialize_message(connection, row[0], "last assistant response")
                if row
                else None
            )

    def find_assistant_message(
        self, session_id: str, run_id: str, turn_id: str
    ) -> MessageRecord | None:
        with self.session_read(session_id) as connection:
            self._require_session(connection, session_id)
            row = connection.execute(
                "SELECT payload FROM messages WHERE session_id = ? "
                "AND run_id = ? "
                "AND json_extract(payload, '$.turn_id') = ? "
                "AND json_extract(payload, '$.role') = 'assistant' "
                "ORDER BY row_id DESC LIMIT 1",
                (session_id, run_id, turn_id),
            ).fetchone()
            return (
                self._materialize_message(connection, row[0], "assistant response") if row else None
            )

    def find_context_provider_message(
        self, session_id: str, run_id: str, delivery_key: str
    ) -> MessageRecord | None:
        with self.session_read(session_id) as connection:
            self._require_session(connection, session_id)
            row = connection.execute(
                "SELECT payload FROM messages WHERE session_id = ? "
                "AND run_id = ? "
                "AND json_extract(payload, '$.metadata.context_provider_delivery_key') = ? "
                "ORDER BY row_id DESC LIMIT 1",
                (session_id, run_id, delivery_key),
            ).fetchone()
            return (
                self._materialize_message(connection, row[0], "context provider delivery")
                if row
                else None
            )

    def find_queued_input_message(self, session_id: str, queue_id: str) -> MessageRecord | None:
        with self.session_read(session_id) as connection:
            self._require_session(connection, session_id)
            row = connection.execute(
                "SELECT payload FROM messages WHERE session_id = ? "
                "AND json_extract(payload, '$.input_ref.input_id') = ? "
                "ORDER BY row_id DESC LIMIT 1",
                (session_id, queue_id),
            ).fetchone()
            return (
                self._materialize_message(connection, row[0], f"queued input {queue_id}")
                if row
                else None
            )

    def load_run(self, session_id: str, run_id: str) -> RunRecord:
        with self._read_connection() as connection:
            return read_run(connection, session_id, run_id)

    def load_run_request_counts(self, session_id: str) -> tuple[RunRequestCounts, ...]:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return read_request_counts(connection, session_id)

    def load_runs(self, session_id: str) -> list[RunRecord]:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return self._load_runs(connection, session_id)

    def list_sessions(self) -> list[SessionRecord]:
        with self._read_connection() as connection:
            return [
                read_session(connection, row[0])
                for row in connection.execute(
                    "SELECT session_id FROM sessions ORDER BY session_id"
                ).fetchall()
            ]

    def load_resource_references(self, session_id: str) -> list[SessionResourceReference]:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return sqlite_resources.read_references(connection, session_id)

    def load_resource_reference(
        self, session_id: str, resource_id: str
    ) -> SessionResourceReference:
        with self._read_connection() as connection:
            self._require_session(connection, session_id)
            return sqlite_resources.read_reference(connection, session_id, resource_id)

    def attach_resource_reference(
        self, session_id: str, reference: SessionResourceReference
    ) -> None:
        with self.session_mutation(session_id):
            connection = self._transaction_connection()
            self._require_session(connection, session_id)
            sqlite_resources.attach_reference(connection, session_id, reference)

    def remove_resource_reference(self, session_id: str, resource_id: str) -> None:
        with self.session_mutation(session_id):
            sqlite_resources.remove_reference(
                self._transaction_connection(), session_id, resource_id
            )

    def load_session_extension_states(self, session_id: str) -> list[SessionExtensionStateRecord]:
        return list(self.load_harness(session_id).state.states)

    def refresh_catalog(
        self,
        session_id: str,
    ) -> str | None:
        try:
            with self.session_mutation(session_id):
                connection = self._transaction_connection()
                entries = self._scan_catalog(connection, session_id=session_id)
                if entries:
                    connection.execute(
                        "INSERT INTO session_catalog(session_id, payload) VALUES(?, ?) "
                        "ON CONFLICT(session_id) DO UPDATE SET payload = excluded.payload",
                        (session_id, entries[0].model_dump_json()),
                    )
                else:
                    connection.execute(
                        "DELETE FROM session_catalog WHERE session_id = ?",
                        (session_id,),
                    )
        except (OSError, PersistenceError, sqlite3.Error, ValueError) as exc:
            return str(exc)
        return None

    def read_catalog(self) -> SessionIndex:
        with self._read_connection() as connection:
            rows = connection.execute("SELECT payload, session_id FROM session_catalog").fetchall()
        entries: list[SessionIndexEntry] = []
        for payload, session_id in rows:
            try:
                entry = SessionIndexEntry.model_validate_json(payload)
            except (ValidationError, ValueError, TypeError) as exc:
                try:
                    raw_entry = json.loads(payload)
                except json.JSONDecodeError, TypeError:
                    raw_entry = None
                entry = invalid_session_index_entry(
                    raw_entry,
                    error=str(exc),
                    fallback_session_id=str(session_id),
                    error_scope="catalog",
                )
            entries.append(entry)
        return SessionIndex(sessions=sort_catalog_entries(entries))

    def rebuild_catalog(self) -> SessionIndex:
        """Explicitly rebuild the disposable projection from canonical rows."""

        with self.session_mutation("catalog_rebuild"):
            connection = self._transaction_connection()
            entries = self._scan_catalog(connection)
            self._replace_catalog(connection, entries)
        return SessionIndex(sessions=sort_catalog_entries(entries))

    def acquire_session_lock(self, session_id: str, run_id: str) -> SessionRunLock | None:
        lock = NativeSessionRunLock(
            self._lock_path(session_id),
            session_id=session_id,
            run_id=run_id,
            connections=self._connections,
        )
        try:
            return lock if lock.acquire() else None
        except OSError as exc:
            raise PersistenceError(
                f"could not acquire SQLite session lock for {session_id}: {exc}"
            ) from exc

    @contextmanager
    def session_read(self, session_id: str) -> Iterator[sqlite3.Connection]:
        # SQLite snapshots protect records, while the existing resource namespace
        # lock keeps their external files alive until selected bytes are owned.
        previous_session = getattr(self._read_state, "session_id", None)
        resources = (
            nullcontext() if previous_session == session_id else self._resource_lock(session_id)
        )
        with resources, self._read_connection() as connection:
            previous = getattr(self._read_state, "connection", None)
            self._read_state.connection = connection
            self._read_state.session_id = session_id
            try:
                yield connection
            finally:
                self._read_state.connection = previous
                self._read_state.session_id = previous_session

    def _resource_lock(self, session_id: str) -> FileMutationLock:
        identity = _validated_record_id(session_id, kind="session")
        return FileMutationLock(self.database.parent / "locks" / "resources" / f"{identity}.lock")

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

    def _scan_catalog(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str | None = None,
    ) -> list[SessionIndexEntry]:
        parameters: tuple[object, ...] = ()
        where = ""
        if session_id is not None:
            where = " WHERE s.session_id = ?"
            parameters = (session_id,)
        rows = connection.execute(
            "SELECT s.session_id, s.payload, "
            "(SELECT m.payload FROM messages m WHERE m.session_id = s.session_id "
            "AND CASE WHEN json_valid(m.payload) "
            "THEN json_extract(m.payload, '$.role') END IN ('user', 'assistant') "
            "AND COALESCE(CASE WHEN json_valid(m.payload) "
            "THEN json_extract(m.payload, '$.scaffold') END, 0) = 0 "
            "ORDER BY m.row_id DESC LIMIT 1), "
            "(SELECT r.payload FROM runs r WHERE r.session_id = s.session_id "
            "ORDER BY r.started_at DESC, r.run_id DESC LIMIT 1) "
            f"FROM sessions s{where} ORDER BY s.session_id",
            parameters,
        ).fetchall()
        entries: list[SessionIndexEntry] = []
        for row in rows:
            session_id = str(row[0])
            try:
                session = self._validate_model(
                    SessionRecord,
                    row[1],
                    f"session {session_id}",
                )
            except UnsupportedDataVersionError as exc:
                entries.append(
                    invalid_session_index_entry(
                        _json_object(row[1]),
                        fallback_session_id=session_id,
                        health="unsupported_version",
                        error=str(exc),
                    )
                )
                continue
            except PersistenceError as exc:
                entries.append(
                    invalid_session_index_entry(
                        _json_object(row[1]),
                        fallback_session_id=session_id,
                        error=str(exc),
                    )
                )
                continue
            try:
                latest_message = None
                if row[2] is not None:
                    fact = decode_versioned_record_json(
                        MessageFact, row[2], label=f"latest message for catalog {session_id}"
                    )
                    record = (
                        read_input(connection, session_id, fact.input_id)
                        if fact.input_id is not None
                        else None
                    )
                    # Catalog text has no use for Call parameters or Outcome bodies.
                    latest_message = MessageRecord.model_validate(
                        {**fact.model_dump(), **resolve_input_content(fact, record)}
                    )
                latest_run = (
                    None
                    if row[3] is None
                    else self._validate_model(
                        RunRecord,
                        row[3],
                        f"latest Run for catalog {session_id}",
                    )
                )
            except UnsupportedDataVersionError as exc:
                entries.append(
                    catalog_entry_from_session(
                        session,
                        health="unsupported_version",
                        error_detail=str(exc),
                    )
                )
                continue
            except PersistenceError as exc:
                entries.append(
                    catalog_entry_from_session(
                        session,
                        health="corrupt",
                        error_detail=str(exc),
                    )
                )
                continue
            entries.append(
                catalog_entry_from_projection(
                    session,
                    SessionCatalogProjection(
                        latest_run=latest_run,
                        latest_message=latest_message,
                        message_count=session.message_count,
                        run_count=session.run_count,
                        resource_count=session.resource_count,
                    ),
                )
            )
        return entries

    @staticmethod
    def _replace_catalog(
        connection: sqlite3.Connection,
        entries: list[SessionIndexEntry],
    ) -> None:
        connection.execute("DELETE FROM session_catalog")
        connection.executemany(
            "INSERT INTO session_catalog(session_id, payload) VALUES(?, ?)",
            [(entry.session_id, entry.model_dump_json()) for entry in entries],
        )

    @staticmethod
    def _raw_trash_tree_rows(
        connection: sqlite3.Connection,
        table: str,
        *,
        session_ids: tuple[str, ...],
        key: str,
    ) -> dict[str, object]:
        placeholders = ", ".join("?" for _ in session_ids)
        cursor = connection.execute(
            f"SELECT * FROM {table} WHERE {key} IN ({placeholders})",  # noqa: S608
            session_ids,
        )
        return {
            "columns": [item[0] for item in cursor.description],
            "rows": [list(row) for row in cursor.fetchall()],
        }

    def _load_runs(self, connection: sqlite3.Connection, session_id: str) -> list[RunRecord]:
        return self._load_models(
            connection,
            "SELECT payload FROM runs WHERE session_id = ? ORDER BY started_at, run_id",
            (session_id,),
            RunRecord,
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

    def _load_models(
        self,
        connection: sqlite3.Connection,
        query: str,
        parameters: tuple[Any, ...],
        model_type: type[Any],
    ) -> list[Any]:
        rows = connection.execute(query, parameters)
        if model_type is MessageRecord:
            return self._materialize_messages(connection, (row[0] for row in rows), "message")
        return [self._validate_model(model_type, row[0], model_type.__name__) for row in rows]

    def _materialize_messages(
        self,
        connection: sqlite3.Connection,
        payloads: Iterable[str],
        label: str,
        *,
        include_media: bool = True,
    ) -> list[MessageRecord]:
        messages = resolve_messages(
            (decode_message_content(payload, label=label) for payload in payloads),
            connection=connection,
            read_input=lambda session_id, input_id: read_input(connection, session_id, input_id),
        )
        return [
            materialize_message_media(
                message,
                partial(self._read_media, message.session_id),
            )
            if include_media
            else message
            for message in messages
        ]

    def _materialize_message(
        self, connection: sqlite3.Connection, payload: str, label: str
    ) -> MessageRecord:
        message = self._resolve_message(connection, payload, label)
        return materialize_message_media(
            message, lambda identity: self._read_media(message.session_id, identity)
        )

    @staticmethod
    def _resolve_message(connection: sqlite3.Connection, payload: str, label: str) -> MessageRecord:
        fact = decode_message_content(payload, label=label)
        reference = fact.outcome_ref
        return materialize_owned_message(
            fact,
            read_input(connection, fact.session_id, fact.input_ref.input_id)
            if fact.input_ref is not None
            else None,
            calls=[
                call
                for identity in fact.referenced_call_ids
                if (
                    call := read_fact(
                        connection,
                        fact.session_id,
                        identity,
                        ToolCallRecord,
                        include_arguments=bool(fact.call_refs),
                    )
                )
                is not None
            ],
            outcome=read_fact(
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

    def _read_media(self, session_id: str, resource_id: str) -> bytes:
        return read_resource_content(
            session_resource_directory(self.database.parent, session_id),
            self.load_resource_reference(session_id, resource_id),
        )

    @staticmethod
    def _validate_model(model_type: type[Any], payload: str, label: str) -> Any:
        if issubclass(model_type, VersionedOuroModel):
            return decode_versioned_record_json(model_type, payload, label=label)
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
            delegation.parent_run_id,
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
        return self._connections.borrow()

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        state = getattr(self._transaction_state, "value", None)
        if state is not None:
            if state.failed:
                raise PersistenceError(
                    "SQLite mutation failed; its uncommitted state is unreadable"
                )
            yield state.connection
            return
        read_connection = getattr(self._read_state, "connection", None)
        if read_connection is not None:
            yield read_connection
            return
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            # A shared connection alone is not a snapshot boundary in SQLite:
            # without an explicit transaction each SELECT may observe a newer
            # commit. Observation snapshots issue several SELECTs and publish a
            # fact position, so they must all read one database version or a
            # concurrent change can be missed by both snapshot and catch-up.
            connection.execute("BEGIN")
            try:
                yield connection
            finally:
                if connection.in_transaction:
                    connection.rollback()
        except sqlite3.Error as exc:
            raise PersistenceError(f"SQLite read failed: {exc}") from exc
        finally:
            if connection is not None:
                self._connections.return_connection(connection)

    def _retire_resource_directory(self, session_id: str, trash_id: str) -> None:
        source = session_resource_directory(self.database.parent, session_id).parent
        if not source.exists():
            return
        target = self.database.parent / "trash" / "sessions" / trash_id / session_id / "resources"
        if any(path.is_symlink() or path.is_junction() for path in (source, *source.parents)):
            raise PersistenceError("Session resource directory contains a symbolic link")
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        state: _TransactionState = self._transaction_state.value
        state.file_moves.append((source, target))

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
        self.file_moves: list[tuple[Path, Path]] = []
        self.resource_locks = ExitStack()

    def restore_files(self) -> None:
        for source, target in reversed(self.file_moves):
            if source.exists():
                # Keep uncommitted replacement bytes for diagnosis without touching old data.
                source.rename(target.with_name(new_id("uncommitted")))
            source.parent.mkdir(parents=True, exist_ok=True)
            target.rename(source)


class _SQLiteMutation(AbstractContextManager[object, None]):
    def __init__(
        self,
        store: SQLiteSessionStore,
        *,
        durable: bool = True,
    ) -> None:
        self.store = store
        self.durable = durable
        self._outermost = False

    def __enter__(self) -> Self:
        state = getattr(self.store._transaction_state, "value", None)
        if state is not None:
            state.depth += 1
            return self
        connection = self.store._connect()
        try:
            if self.durable:
                # synchronous is connection-local. Set FULL explicitly rather
                # than relying on the sqlite build's default for canonical
                # mutation boundaries.
                connection.execute("PRAGMA synchronous = FULL")
            else:
                # Only the current response Draft uses weak persistence.
                # Business commits always use FULL, including nested appends.
                connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            self.store._connections.return_connection(connection)
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
                state.restore_files()
            else:
                state.connection.commit()
        except sqlite3.Error as error:
            state.connection.rollback()
            state.restore_files()
            raise PersistenceError(f"SQLite transaction failed: {error}") from error
        finally:
            self.store._connections.return_connection(state.connection)
            state.resource_locks.close()
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


def _json_object(payload: str) -> object:
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None
