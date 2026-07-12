from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from ouro.application.dto import Diagnostic
from ouro.application.session_history import (
    copy_branch_messages,
    project_replay_frames,
    select_branch_messages,
)
from ouro.application.session_models import (
    SessionBranchResult,
    SessionCatalogEntry,
    SessionCatalogQuery,
    SessionCatalogResult,
    SessionDetail,
    SessionMutationResult,
    SessionReplay,
    SessionTrashResult,
    SessionView,
)
from ouro.core.base import new_id, utc_now
from ouro.core.errors import (
    PersistenceError,
    SessionBranchError,
    SessionBusyError,
    SessionNotFoundError,
)
from ouro.core.messages import MessageRecord
from ouro.core.state import SessionRecord, SessionTitleSource
from ouro.sessions import SessionIndexEntry, SessionSnapshot, SessionStore

if TYPE_CHECKING:
    from ouro.application.resources import ResourceService

_MAX_TITLE_LENGTH = 160


class SessionTitleGenerator(Protocol):
    async def __call__(
        self,
        session: SessionView,
        messages: tuple[MessageRecord, ...],
    ) -> str: ...


class SessionService:
    """Application boundary for canonical session commands and catalog queries."""

    def __init__(
        self,
        store: SessionStore,
        *,
        resources: ResourceService | None = None,
    ) -> None:
        self._store = store
        self._resources = resources

    def create(
        self,
        *,
        title: str = "New session",
        title_source: SessionTitleSource = "fallback",
        workspace: str | None = None,
        current_model: str | None = None,
    ) -> SessionMutationResult:
        normalized_title = _normalize_title(title)
        session = SessionRecord(
            title=normalized_title,
            title_source=title_source,
            title_locked=title_source == "user",
            workspace=workspace,
            current_model=current_model,
        )
        self._store.create_session(session)
        return SessionMutationResult(
            session=SessionView.from_record(session),
            diagnostics=self._refresh_index_diagnostics(session.session_id),
        )

    def open(self, session_id: str, *, recover: bool = True) -> SessionDetail:
        snapshot = self._load_snapshot(session_id, recover=recover)
        diagnostics = self._refresh_index_diagnostics(session_id) if recover else ()
        return SessionDetail(
            session=SessionView.from_record(snapshot.session),
            messages=tuple(snapshot.messages),
            events=tuple(snapshot.events),
            runs=tuple(snapshot.runs),
            compactions=tuple(snapshot.compactions),
            diagnostics=diagnostics,
        )

    def catalog(self, query: SessionCatalogQuery | None = None) -> SessionCatalogResult:
        diagnostics: tuple[Diagnostic, ...] = ()
        try:
            index = self._store.load_index()
        except PersistenceError as exc:
            index = self._store.scan_index()
            diagnostics = (
                Diagnostic(
                    code="session_index_unavailable",
                    severity="warning",
                    component="sessions.catalog",
                    message=str(exc),
                    recoverable=True,
                    suggested_action="Rebuild the session index.",
                ),
            )
        entries = _filter_entries(index.sessions, query or SessionCatalogQuery())
        return SessionCatalogResult(
            entries=tuple(SessionCatalogEntry.from_index(entry) for entry in entries),
            diagnostics=diagnostics,
        )

    def rename(self, session_id: str, title: str) -> SessionMutationResult:
        return self._update_session(
            session_id,
            title=_normalize_title(title),
            title_source="user",
            title_locked=True,
        )

    def set_archived(self, session_id: str, archived: bool) -> SessionMutationResult:
        return self._update_session(session_id, archived=archived)

    def set_pinned(self, session_id: str, pinned: bool) -> SessionMutationResult:
        return self._update_session(session_id, pinned=pinned)

    def set_tags(self, session_id: str, tags: Iterable[str]) -> SessionMutationResult:
        normalized = sorted({tag.strip() for tag in tags if tag.strip()})
        return self._update_session(session_id, tags=normalized)

    async def generate_title(
        self,
        session_id: str,
        generator: SessionTitleGenerator,
    ) -> SessionMutationResult:
        snapshot = self._load_snapshot(session_id, recover=False)
        if snapshot.session.title_locked or snapshot.session.title_source == "user":
            return SessionMutationResult(session=SessionView.from_record(snapshot.session))
        try:
            generated = await generator(
                SessionView.from_record(snapshot.session),
                tuple(snapshot.messages),
            )
            title = _normalize_title(generated)
        except Exception as exc:  # noqa: BLE001 - optional title provider isolation boundary
            return SessionMutationResult(
                session=SessionView.from_record(snapshot.session),
                diagnostics=(
                    Diagnostic(
                        code="session_title_generation_failed",
                        severity="warning",
                        component="sessions.title",
                        message=f"title generator failed ({type(exc).__name__})",
                        session_id=session_id,
                        recoverable=True,
                        suggested_action="Retry title generation or rename the session manually.",
                    ),
                ),
            )

        try:
            latest = self._load_snapshot(session_id, recover=False).session
            if latest.title_locked or latest.title_source == "user":
                return SessionMutationResult(session=SessionView.from_record(latest))
            return self._update_session(
                session_id,
                title=title,
                title_source="generated",
                title_locked=False,
            )
        except SessionBusyError as exc:
            latest = self._load_snapshot(session_id, recover=False).session
            return SessionMutationResult(
                session=SessionView.from_record(latest),
                diagnostics=(
                    Diagnostic(
                        code="session_title_update_deferred",
                        severity="info",
                        component="sessions.title",
                        message=str(exc),
                        session_id=session_id,
                        recoverable=True,
                        suggested_action="Retry after the active run settles.",
                    ),
                ),
            )

    def trash(self, session_id: str) -> SessionTrashResult:
        snapshot = self._load_snapshot(session_id, recover=False)
        resource_ids: list[str] = []
        lock = self._store.acquire_session_lock(session_id, new_id("trash"))
        if lock is None:
            raise SessionBusyError(f"session already has an active run: {session_id}")
        try:
            session = self._load_snapshot(session_id, recover=False).session
            if session.status in {"running", "waiting_approval", "waiting_user"}:
                raise SessionBusyError(f"session has unfinished work: {session_id}")
            if self._resources is not None:
                resource_ids = list(self._resources.session_resource_ids(session_id))
            trash_id = self._store.trash_session(session_id)
        finally:
            lock.release()
        diagnostics = list(self._refresh_index_diagnostics(session_id))
        if self._resources is not None and resource_ids:
            try:
                self._resources.collect_after_session_trash(resource_ids)
            except Exception as exc:  # noqa: BLE001 - trash is canonical; GC is retryable.
                diagnostics.append(
                    Diagnostic(
                        code="resource_collection_deferred",
                        severity="warning",
                        component="resources.gc",
                        message=f"resource collection deferred ({type(exc).__name__})",
                        session_id=snapshot.session.session_id,
                        recoverable=True,
                        suggested_action="Retry resource garbage collection.",
                    )
                )
        return SessionTrashResult(
            session_id=session_id,
            trash_id=trash_id,
            diagnostics=tuple(diagnostics),
        )

    def branch(self, source_session_id: str, message_id: str) -> SessionBranchResult:
        source = self._load_snapshot(source_session_id, recover=False)
        selected_messages, source_message = select_branch_messages(
            source.messages,
            message_id,
        )
        resource_ids = list(
            dict.fromkeys(
                resource_id
                for message in selected_messages
                for resource_id in message.resource_refs
            )
        )
        if resource_ids and self._resources is None:
            raise SessionBranchError(
                "branch contains resource references; ResourceService must copy their records "
                "before this branch can be published"
            )

        now = utc_now()
        branch = SessionRecord(
            root_session_id=source.session.root_session_id or source.session.session_id,
            parent_session_id=source.session.session_id,
            forked_from_message_id=source_message.message_id,
            forked_from_run_id=source_message.run_id,
            title=_branch_title(source.session.title),
            title_source="fallback",
            title_locked=False,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
            workspace=source.session.workspace,
            workspace_trusted=source.session.workspace_trusted,
            current_model=source.session.current_model,
            tags=list(source.session.tags),
        )
        copied_messages, message_id_map = copy_branch_messages(
            branch.session_id,
            selected_messages,
        )
        branch = branch.model_copy(
            update={
                "last_message_id": copied_messages[-1].message_id if copied_messages else None,
                "message_count": len(copied_messages),
            }
        )
        resource_references = (
            self._resources.branch_references(
                source_session_id,
                branch.session_id,
                resource_ids,
            )
            if self._resources is not None
            else []
        )
        self._store.create_session_snapshot(branch, copied_messages, resource_references)
        return SessionBranchResult(
            session=SessionView.from_record(branch),
            source_session_id=source_session_id,
            source_message_id=source_message.message_id,
            message_id_map=message_id_map,
            diagnostics=self._refresh_index_diagnostics(branch.session_id),
        )

    def replay(self, session_id: str, *, run_id: str | None = None) -> SessionReplay:
        snapshot = self._load_snapshot(session_id, recover=False)
        events = snapshot.events
        if run_id is not None:
            events = [event for event in events if event.run_id == run_id]
        return SessionReplay(
            session_id=session_id,
            run_id=run_id,
            frames=project_replay_frames(events),
        )

    def _update_session(self, session_id: str, **changes: object) -> SessionMutationResult:
        self._load_snapshot(session_id, recover=False)
        lock = self._store.acquire_session_lock(session_id, new_id("session"))
        if lock is None:
            raise SessionBusyError(f"session already has an active run: {session_id}")
        try:
            current = self._load_snapshot(session_id, recover=False).session
            now = utc_now()
            updated = current.model_copy(
                update={
                    **changes,
                    "updated_at": now,
                    "last_activity_at": now,
                }
            )
            self._store.save_session(updated)
        finally:
            lock.release()
        return SessionMutationResult(
            session=SessionView.from_record(updated),
            diagnostics=self._refresh_index_diagnostics(session_id),
        )

    def _refresh_index_diagnostics(self, session_id: str) -> tuple[Diagnostic, ...]:
        error = self._store.try_update_index(session_id)
        if error is None:
            return ()
        return (
            Diagnostic(
                code="session_index_refresh_failed",
                severity="warning",
                component="sessions.catalog",
                message=error,
                session_id=session_id,
                recoverable=True,
                suggested_action="Rebuild the session index.",
            ),
        )

    def _load_snapshot(self, session_id: str, *, recover: bool) -> SessionSnapshot:
        try:
            return self._store.load_session(session_id, recover=recover)
        except FileNotFoundError as exc:
            raise SessionNotFoundError(f"session not found: {session_id}") from exc


def _normalize_title(value: str) -> str:
    title = " ".join(value.split())
    if not title:
        raise ValueError("session title cannot be empty")
    if len(title) > _MAX_TITLE_LENGTH:
        raise ValueError(f"session title exceeds {_MAX_TITLE_LENGTH} characters")
    return title


def _branch_title(source_title: str) -> str:
    suffix = " · 分支"
    normalized = _normalize_title(source_title)
    if len(normalized) + len(suffix) <= _MAX_TITLE_LENGTH:
        return f"{normalized}{suffix}"
    return f"{normalized[: _MAX_TITLE_LENGTH - len(suffix)].rstrip()}{suffix}"


def _filter_entries(
    entries: Iterable[SessionIndexEntry],
    query: SessionCatalogQuery,
) -> list[SessionIndexEntry]:
    filtered = [entry for entry in entries if _matches_query(entry, query)]
    if query.sort == "created_desc":
        return sorted(
            filtered,
            key=lambda entry: (entry.created_at, entry.session_id),
            reverse=True,
        )
    if query.sort == "title":
        return sorted(filtered, key=lambda entry: (entry.title.casefold(), entry.session_id))
    return sorted(
        filtered,
        key=lambda entry: (entry.last_activity_at, entry.session_id),
        reverse=True,
    )


def _matches_query(entry: SessionIndexEntry, query: SessionCatalogQuery) -> bool:
    if query.statuses and entry.status not in query.statuses:
        return False
    if query.archived is not None and entry.archived is not query.archived:
        return False
    if query.pinned is not None and entry.pinned is not query.pinned:
        return False
    if query.workspace is not None and entry.workspace != query.workspace:
        return False
    if query.model is not None and entry.current_model != query.model:
        return False
    if query.tags and not query.tags.issubset(entry.tags):
        return False
    if query.health and entry.health not in query.health:
        return False
    if not _inside_range(entry.created_at, query.created_after, query.created_before):
        return False
    if not _inside_range(entry.updated_at, query.updated_after, query.updated_before):
        return False
    if query.text:
        needle = query.text.casefold().strip()
        fields = (
            entry.title,
            entry.last_message_preview,
            entry.search_text,
            entry.workspace or "",
            entry.current_model or "",
            " ".join(entry.tags),
        )
        if needle and not any(needle in field.casefold() for field in fields):
            return False
    return True


def _inside_range(value: datetime, lower: datetime | None, upper: datetime | None) -> bool:
    return (lower is None or value >= lower) and (upper is None or value <= upper)
