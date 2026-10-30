from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from ouro.core.base import new_id, utc_now
from ouro.core.errors import PersistenceError, UnsupportedDataVersionError
from ouro.core.inputs import resolve_input_content
from ouro.core.messages import MessageRecord
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.state import SessionRecord
from ouro.local_io import write_model_atomic
from ouro.local_locks import FileOwnerLock
from ouro.sessions.backends.files.database import connect_data
from ouro.sessions.backends.files.inputs import input_path, read_input
from ouro.sessions.backends.files.journal import reverse_jsonl
from ouro.sessions.backends.files.messages import FileMessageRecords
from ouro.sessions.catalog import (
    SessionCatalogProjection,
    catalog_entry_from_projection,
    catalog_entry_from_session,
    sort_catalog_entries,
)
from ouro.sessions.index import (
    SessionHealth,
    SessionIndex,
    SessionIndexEntry,
    invalid_session_index_entry,
)
from ouro.sessions.sqlite_connections import ExecutionConnections
from ouro.sessions.sqlite_runs import read_run

_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_MAX_PREVIEW_SCAN_BYTES = 64 * 1024


class LocalSessionIndex:
    """Disposable local catalog built only from canonical session artifacts."""

    def __init__(
        self,
        sessions_dir: Path,
        locks_dir: Path,
        connections: Mapping[str, ExecutionConnections],
    ) -> None:
        self.sessions_dir = sessions_dir
        self.index_path = sessions_dir / "index.json"
        self.lock_path = locks_dir / "catalog.lock"
        self._connections = connections

    def try_initialize(self) -> str | None:
        """Create the initial empty projection without reading Session history."""

        try:
            if self.index_path.exists():
                return None
            has_sessions = any(
                path.is_dir() and not path.name.startswith(".")
                for path in self.sessions_dir.iterdir()
            )
            if has_sessions:
                return f"session index is missing and requires rebuild: {self.index_path}"
            write_model_atomic(self.index_path, SessionIndex())
        except (OSError, PersistenceError, ValueError) as exc:
            return str(exc)
        return None

    def scan(self) -> SessionIndex:
        entries = [
            self._build_entry(session_dir)
            for session_dir in sorted(self.sessions_dir.iterdir())
            if session_dir.is_dir() and not session_dir.name.startswith(".")
        ]
        return SessionIndex(sessions=sort_catalog_entries(entries))

    def rebuild(self) -> SessionIndex:
        lock = self._new_lock()
        if not lock.acquire():
            raise PersistenceError("session index is busy")
        try:
            return self._write(self.scan())
        finally:
            lock.release()

    def try_update(
        self,
        session_id: str,
    ) -> str | None:
        """Refresh one projection entry without scanning unrelated Sessions."""

        if not _RECORD_ID.fullmatch(session_id):
            return f"invalid session id: {session_id!r}"
        lock = self._new_lock()
        try:
            if not lock.acquire():
                return "session index is busy"
            index = self.load()
            entries = [entry for entry in index.sessions if entry.session_id != session_id]
            session_dir = self.sessions_dir / session_id
            if session_dir.is_dir():
                entries.append(self._build_entry(session_dir))
            self._write(SessionIndex(sessions=sort_catalog_entries(entries)))
        except (OSError, PersistenceError, ValueError) as exc:
            return str(exc)
        finally:
            lock.release()
        return None

    def load(self) -> SessionIndex:
        try:
            payload = json.loads(self.index_path.read_text("utf-8"))
            raw_sessions = payload.get("sessions") if isinstance(payload, dict) else None
            if not isinstance(payload, dict) or not isinstance(raw_sessions, list):
                raise ValueError("session index must contain a sessions list")
            header = SessionIndex.model_validate({**payload, "sessions": []})
            entries: list[SessionIndexEntry] = []
            for index, raw_entry in enumerate(raw_sessions):
                try:
                    entry = SessionIndexEntry.model_validate(raw_entry)
                except ValidationError as exc:
                    entry = invalid_session_index_entry(
                        raw_entry,
                        error=str(exc),
                        fallback_session_id=f"invalid_catalog_entry_{index}",
                        error_scope="catalog",
                    )
                entries.append(entry)
            return header.model_copy(update={"sessions": entries})
        except (OSError, ValidationError, ValueError) as exc:
            raise PersistenceError(
                f"could not load session index {self.index_path}; rebuild required: {exc}"
            ) from exc

    def _new_lock(self) -> FileOwnerLock:
        return FileOwnerLock(
            self.lock_path,
            owner={"scope": "session_catalog", "operation_id": new_id("index")},
        )

    def _build_entry(self, session_dir: Path) -> SessionIndexEntry:
        session_id = session_dir.name
        if not _RECORD_ID.fullmatch(session_id):
            return self._corrupt_entry(session_dir)
        try:
            with connect_data(
                session_dir, connections=self._connections.get(session_id)
            ) as connection:
                return self._read_entry(connection, session_dir)
        except (OSError, PersistenceError) as exc:
            return self._corrupt_entry(session_dir, error_detail=str(exc))

    def _read_entry(self, connection: sqlite3.Connection, session_dir: Path) -> SessionIndexEntry:
        session_id = session_dir.name
        row = connection.execute(
            "SELECT payload FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise FileNotFoundError(session_id)
        raw_session = row[0]
        try:
            session = decode_versioned_record_json(
                SessionRecord,
                raw_session,
                label=f"session record {session_id}",
            )
        except UnsupportedDataVersionError as exc:
            return invalid_session_index_entry(
                _json_object(raw_session),
                fallback_session_id=session_id,
                health="unsupported_version",
                error=str(exc),
            )
        except (PersistenceError, ValidationError, ValueError) as exc:
            return invalid_session_index_entry(
                _json_object(raw_session),
                fallback_session_id=session_id,
                error=str(exc),
            )

        try:
            projection = SessionCatalogProjection(
                latest_run=read_run(connection, session_id, session.last_run_id)
                if session.last_run_id is not None
                else None,
                latest_message=self._latest_message(session_dir / "messages.jsonl"),
                message_count=session.message_count,
                run_count=session.run_count,
                resource_count=session.resource_count,
            )
        except UnsupportedDataVersionError as exc:
            return catalog_entry_from_session(
                session,
                health="unsupported_version",
                error_detail=str(exc),
            )
        except (FileNotFoundError, PersistenceError) as exc:
            return catalog_entry_from_session(
                session,
                health="corrupt",
                error_detail=str(exc),
            )
        return catalog_entry_from_projection(session, projection)

    @staticmethod
    def _latest_message(path: Path) -> MessageRecord | None:
        messages = FileMessageRecords()
        directory = path.parent
        scanned = 0
        with closing(reverse_jsonl(path, block_size=8192)) as lines:
            for _, raw in lines:
                scanned += len(raw)
                if scanned > _MAX_PREVIEW_SCAN_BYTES:
                    break
                fact = messages.entry(raw, session_id=directory.name)
                if fact.scaffold or fact.role not in {"user", "assistant"}:
                    continue
                record = None
                if fact.input_id is not None:
                    scanned += input_path(directory, fact.input_id).stat().st_size
                    if scanned > _MAX_PREVIEW_SCAN_BYTES:
                        break
                    record = read_input(directory, directory.name, fact.input_id)
                # The preview uses only owned assistant text or the selected Input body.
                # Original tool parameters and Outcomes do not contribute to this projection.
                message = MessageRecord.model_validate(
                    {**fact.model_dump(), **resolve_input_content(fact, record)}
                )
                if (
                    not message.scaffold
                    and message.role in {"user", "assistant"}
                    and message.text_content
                ):
                    return message
        return None

    def _corrupt_entry(
        self,
        session_dir: Path,
        *,
        health: SessionHealth = "corrupt",
        error_detail: str | None = None,
    ) -> SessionIndexEntry:
        try:
            modified = datetime.fromtimestamp(session_dir.stat().st_mtime, UTC)
        except OSError:
            modified = utc_now()
        return SessionIndexEntry(
            session_id=session_dir.name,
            session_kind="primary",
            root_session_id=session_dir.name,
            title=(
                "Unsupported data version" if health == "unsupported_version" else "Corrupt session"
            ),
            created_at=modified,
            updated_at=modified,
            last_activity_at=modified,
            status="failed",
            health=health,
            error_detail=error_detail,
        )

    def _write(self, index: SessionIndex) -> SessionIndex:
        write_model_atomic(self.index_path, index)
        return index


def _json_object(payload: str) -> object:
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None
