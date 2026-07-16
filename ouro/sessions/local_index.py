from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from ouro.core.base import new_id, utc_now
from ouro.core.errors import PersistenceError
from ouro.core.state import SessionRecord
from ouro.sessions.base import RuntimeSessionSnapshot
from ouro.sessions.catalog import (
    catalog_entry_from_session,
    catalog_entry_from_snapshot,
    sort_catalog_entries,
)
from ouro.sessions.index import SessionIndex, SessionIndexEntry
from ouro.sessions.local_io import write_model_atomic, write_text_atomic
from ouro.sessions.locks import SessionRunLock

_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
SnapshotLoader = Callable[[str], RuntimeSessionSnapshot]


class LocalSessionIndex:
    """Disposable local catalog built only from canonical session artifacts."""

    def __init__(
        self,
        sessions_dir: Path,
        locks_dir: Path,
        load_snapshot: SnapshotLoader,
    ) -> None:
        self.sessions_dir = sessions_dir
        self.index_path = sessions_dir / "index.json"
        self.dirty_path = sessions_dir / ".index-dirty"
        self.lock_path = locks_dir / "catalog.lock"
        self._load_snapshot = load_snapshot

    def try_initialize(self) -> str | None:
        try:
            if not self.index_path.exists():
                write_model_atomic(self.index_path, SessionIndex())
            else:
                self.load()
        except (OSError, PersistenceError, ValueError) as exc:
            self._mark_dirty(str(exc))
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
            self._mark_dirty(str(exc))
            return str(exc)
        return None

    def try_update(
        self,
        session_id: str,
        *,
        snapshot: RuntimeSessionSnapshot | None = None,
    ) -> str | None:
        lock = self._new_lock()
        try:
            if not lock.acquire():
                error = "session index is busy"
                self._mark_dirty(error)
                return error
            if self.dirty_path.exists():
                self._write(self.scan())
                return None

            try:
                index = SessionIndex.model_validate_json(self.index_path.read_text("utf-8"))
            except (OSError, ValidationError, ValueError):
                self._write(self.scan())
                return None

            entries = [entry for entry in index.sessions if entry.session_id != session_id]
            session_dir = self.sessions_dir / session_id
            if session_dir.exists():
                entries.append(
                    self._entry_from_snapshot(snapshot)
                    if snapshot is not None
                    else self._build_entry(session_dir)
                )
            self._write(SessionIndex(sessions=sort_catalog_entries(entries)))
        except (OSError, PersistenceError, ValueError) as exc:
            self._mark_dirty(str(exc))
            return str(exc)
        finally:
            lock.release()
        return None

    def load(self) -> SessionIndex:
        if self.dirty_path.exists():
            try:
                return self.rebuild()
            except (OSError, PersistenceError, ValueError) as exc:
                raise PersistenceError(
                    f"could not refresh dirty session index {self.index_path}: {exc}"
                ) from exc
        try:
            return SessionIndex.model_validate_json(self.index_path.read_text("utf-8"))
        except (OSError, ValidationError, ValueError):
            try:
                return self.rebuild()
            except (OSError, PersistenceError, ValueError) as exc:
                raise PersistenceError(
                    f"could not rebuild session index {self.index_path}: {exc}"
                ) from exc

    def _new_lock(self) -> SessionRunLock:
        return SessionRunLock(
            self.lock_path,
            session_id="catalog",
            run_id=new_id("index"),
        )

    def _build_entry(self, session_dir: Path) -> SessionIndexEntry:
        session_id = session_dir.name
        if not _RECORD_ID.fullmatch(session_id):
            return self._corrupt_entry(session_dir)
        try:
            session = SessionRecord.model_validate_json(
                (session_dir / "session.json").read_text("utf-8")
            )
        except (OSError, ValidationError, ValueError):
            return self._corrupt_entry(session_dir)

        try:
            snapshot = self._load_snapshot(session_id)
        except (FileNotFoundError, PersistenceError):
            return catalog_entry_from_session(session, health="corrupt")
        return self._entry_from_snapshot(snapshot)

    def _entry_from_snapshot(self, snapshot: RuntimeSessionSnapshot) -> SessionIndexEntry:
        resource_count, missing_resource = self._resource_counts(snapshot.session.session_id)
        return catalog_entry_from_snapshot(
            snapshot,
            resource_count=resource_count,
            resource_missing=missing_resource,
        )

    def _corrupt_entry(self, session_dir: Path) -> SessionIndexEntry:
        try:
            modified = datetime.fromtimestamp(session_dir.stat().st_mtime, UTC)
        except OSError:
            modified = utc_now()
        return SessionIndexEntry(
            session_id=session_dir.name,
            root_session_id=session_dir.name,
            title="Corrupt session",
            created_at=modified,
            updated_at=modified,
            last_activity_at=modified,
            status="failed",
            health="corrupt",
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
        with suppress(OSError):
            self.dirty_path.unlink()
        return index

    def _mark_dirty(self, error: str) -> None:
        with suppress(OSError, PersistenceError):
            write_text_atomic(self.dirty_path, error)
