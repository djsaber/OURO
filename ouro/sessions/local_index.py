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
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions.base import SessionSnapshot
from ouro.sessions.index import (
    PendingInteraction,
    SessionCatalogStatus,
    SessionHealth,
    SessionIndex,
    SessionIndexEntry,
)
from ouro.sessions.local_io import write_model_atomic, write_text_atomic
from ouro.sessions.locks import SessionRunLock

_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
SnapshotLoader = Callable[[str], SessionSnapshot]


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
        return SessionIndex(sessions=_sorted_entries(entries))

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

    def try_update(self, session_id: str) -> str | None:
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
                entries.append(self._build_entry(session_dir))
            self._write(SessionIndex(sessions=_sorted_entries(entries)))
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
            return _entry_from_session(session, health="corrupt")
        return self._entry_from_snapshot(snapshot)

    def _entry_from_snapshot(self, snapshot: SessionSnapshot) -> SessionIndexEntry:
        session = snapshot.session
        latest_run = snapshot.runs[-1] if snapshot.runs else None
        status = _catalog_status(session, latest_run)
        health: SessionHealth = "recoverable" if status == "recoverable" else "ok"
        resource_count, missing_resource = self._resource_counts(session.session_id)
        if missing_resource:
            health = "missing_resource"
        last_activity_at = session.last_activity_at
        if snapshot.messages:
            last_activity_at = max(last_activity_at, snapshot.messages[-1].created_at)
        if latest_run is not None:
            last_activity_at = max(last_activity_at, latest_run.ended_at or latest_run.started_at)
        if snapshot.events:
            last_activity_at = max(last_activity_at, snapshot.events[-1].created_at)
        return _entry_from_session(
            session,
            status=status,
            health=health,
            active_run_id=_active_run_id(session, latest_run),
            message_count=len(snapshot.messages),
            run_count=len(snapshot.runs),
            resource_count=resource_count,
            last_message_preview=_last_message_preview(snapshot.messages),
            search_text=_message_search_text(snapshot.messages),
            last_activity_at=last_activity_at,
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
        records_dir = self.sessions_dir.parent / "resources" / "records"
        missing = any(
            not isinstance(item, dict)
            or not isinstance((resource_id := item.get("resource_id")), str)
            or re.fullmatch(r"res_[0-9a-f]{64}", resource_id) is None
            or not (records_dir / f"{resource_id}.json").is_file()
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


def _entry_from_session(
    session: SessionRecord,
    *,
    status: SessionCatalogStatus | None = None,
    health: SessionHealth = "ok",
    active_run_id: str | None = None,
    message_count: int | None = None,
    run_count: int = 0,
    resource_count: int = 0,
    last_message_preview: str = "",
    search_text: str = "",
    last_activity_at: datetime | None = None,
) -> SessionIndexEntry:
    provider, endpoint = _split_model_route(session.current_model)
    pending_interaction: PendingInteraction | None = None
    if session.status == "waiting_approval":
        pending_interaction = "approval"
    elif session.status == "waiting_user":
        pending_interaction = "user"
    return SessionIndexEntry(
        session_id=session.session_id,
        root_session_id=session.root_session_id or session.session_id,
        parent_session_id=session.parent_session_id,
        title=session.title,
        title_source=session.title_source,
        title_locked=session.title_locked,
        created_at=session.created_at,
        updated_at=session.updated_at,
        last_activity_at=last_activity_at or session.last_activity_at,
        status=status or _catalog_status(session, None),
        active_run_id=active_run_id or session.active_run_id,
        pending_interaction=pending_interaction,
        current_provider=provider,
        current_endpoint=endpoint,
        current_model=session.current_model,
        approval_mode=session.approval_mode,
        workspace=session.workspace,
        workspace_trusted=session.workspace_trusted,
        archived=session.archived,
        pinned=session.pinned,
        tags=list(session.tags),
        message_count=session.message_count if message_count is None else message_count,
        run_count=run_count,
        resource_count=resource_count,
        last_message_preview=last_message_preview,
        search_text=search_text,
        health=health,
    )


def _sorted_entries(entries: list[SessionIndexEntry]) -> list[SessionIndexEntry]:
    return sorted(
        entries,
        key=lambda entry: (entry.last_activity_at, entry.session_id),
        reverse=True,
    )


def _catalog_status(session: SessionRecord, latest_run: RunRecord | None) -> SessionCatalogStatus:
    if latest_run is not None and latest_run.status == "recoverable":
        return "recoverable"
    if latest_run is not None and latest_run.status == "paused":
        return "paused"
    if session.status == "running":
        return "active"
    if session.status == "waiting_approval":
        return "waiting_approval"
    if session.status == "waiting_user":
        return "waiting_user"
    if session.status == "failed":
        return "failed"
    if latest_run is not None or session.message_count > 0:
        return "completed"
    return "idle"


def _active_run_id(session: SessionRecord, latest_run: RunRecord | None) -> str | None:
    if session.status not in {"running", "waiting_approval", "waiting_user"}:
        return None
    if session.active_run_id is not None:
        return session.active_run_id
    return latest_run.run_id if latest_run is not None else session.last_run_id


def _split_model_route(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    provider, separator, endpoint = value.partition("/")
    if not separator:
        return None, None
    return provider or None, endpoint or None


def _last_message_preview(messages: list[MessageRecord], *, limit: int = 160) -> str:
    for message in reversed(messages):
        if message.role not in {"user", "assistant"}:
            continue
        text = " ".join(message.text_content.split())
        if text:
            return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"
    return ""


def _message_search_text(messages: list[MessageRecord]) -> str:
    return "\n".join(
        message.text_content
        for message in messages
        if message.role in {"user", "assistant"} and message.text_content
    )
