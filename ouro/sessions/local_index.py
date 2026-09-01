from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from ouro.core.base import new_id, utc_now
from ouro.core.errors import MigrationRequiredError, PersistenceError
from ouro.core.messages import MessageRecord
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.state import RunRecord, SessionRecord
from ouro.local_io import write_model_atomic
from ouro.local_locks import FileOwnerLock
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

_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_MAX_PREVIEW_SCAN_BYTES = 64 * 1024


class LocalSessionIndex:
    """Disposable local catalog built only from canonical session artifacts."""

    def __init__(
        self,
        sessions_dir: Path,
        locks_dir: Path,
    ) -> None:
        self.sessions_dir = sessions_dir
        self.index_path = sessions_dir / "index.json"
        self.lock_path = locks_dir / "catalog.lock"

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

    def try_rebuild(self) -> str | None:
        try:
            self.rebuild()
        except (OSError, PersistenceError, ValueError) as exc:
            return str(exc)
        return None

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
            raw_session = (session_dir / "session.json").read_text("utf-8")
        except OSError as exc:
            return self._corrupt_entry(session_dir, error_detail=str(exc))
        try:
            session = decode_versioned_record_json(
                SessionRecord,
                raw_session,
                label=f"session record {session_id}",
            )
        except MigrationRequiredError as exc:
            return invalid_session_index_entry(
                _json_object(raw_session),
                fallback_session_id=session_id,
                health="migration_required",
                error=str(exc),
            )
        except (PersistenceError, ValidationError, ValueError) as exc:
            return invalid_session_index_entry(
                _json_object(raw_session),
                fallback_session_id=session_id,
                error=str(exc),
            )

        try:
            projection = self._load_projection(session_dir, session)
        except MigrationRequiredError as exc:
            return catalog_entry_from_session(
                session,
                health="migration_required",
                error_detail=str(exc),
            )
        except (FileNotFoundError, PersistenceError) as exc:
            return catalog_entry_from_session(
                session,
                health="corrupt",
                error_detail=str(exc),
            )
        return catalog_entry_from_projection(session, projection)

    def _load_projection(
        self,
        session_dir: Path,
        session: SessionRecord,
    ) -> SessionCatalogProjection:
        resource_count, missing_resource = self._resource_counts(session.session_id)
        run_count = sum(1 for _ in (session_dir / "runs").glob("*.json"))
        latest_run = None
        if session.last_run_id is not None:
            latest_run = decode_versioned_record_json(
                RunRecord,
                (session_dir / "runs" / f"{session.last_run_id}.json").read_text("utf-8"),
                label=f"latest Run for catalog {session.session_id}",
            )
        return SessionCatalogProjection(
            latest_run=latest_run,
            latest_message=self._latest_message(session_dir / "messages.jsonl"),
            message_count=session.message_count,
            run_count=run_count,
            resource_count=resource_count,
            resource_missing=missing_resource,
        )

    @staticmethod
    def _latest_message(path: Path) -> MessageRecord | None:
        with path.open("rb") as stream:
            end = stream.seek(0, 2)
            scanned = 0
            buffer = b""
            while end > 0 and scanned < _MAX_PREVIEW_SCAN_BYTES:
                size = min(8192, end, _MAX_PREVIEW_SCAN_BYTES - scanned)
                end -= size
                scanned += size
                stream.seek(end)
                buffer = stream.read(size) + buffer
                lines = buffer.split(b"\n")
                complete_lines = lines[1:] if end > 0 else lines
                for raw in reversed(complete_lines):
                    if not raw.strip():
                        continue
                    message = decode_versioned_record_json(
                        MessageRecord,
                        raw,
                        label="message for catalog preview",
                    )
                    if (
                        not message.scaffold
                        and message.role in {"user", "assistant"}
                        and message.text_content
                    ):
                        return message
                buffer = lines[0] if end > 0 else b""
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
            title=("Migration required" if health == "migration_required" else "Corrupt session"),
            created_at=modified,
            updated_at=modified,
            last_activity_at=modified,
            status="failed",
            health=health,
            error_detail=error_detail,
        )

    def _resource_counts(self, session_id: str) -> tuple[int, bool]:
        path = self.sessions_dir / session_id / "resources" / "index.json"
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0, True
        resources = raw.get("resources") if isinstance(raw, dict) else None
        if not isinstance(resources, list):
            return 0, True
        missing = any(
            not isinstance(item, dict)
            or not isinstance((resource_id := item.get("resource_id")), str)
            or re.fullmatch(r"res_[0-9a-f]{64}", resource_id) is None
            for item in resources
        )
        return len(resources), missing

    def _write(self, index: SessionIndex) -> SessionIndex:
        write_model_atomic(self.index_path, index)
        return index


def _json_object(payload: str) -> object:
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None
