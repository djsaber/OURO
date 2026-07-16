from __future__ import annotations

from datetime import datetime

from ouro.core.messages import MessageRecord
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord, SessionRecord
from ouro.sessions.base import RuntimeSessionSnapshot
from ouro.sessions.index import (
    PendingInteraction,
    SessionCatalogStatus,
    SessionHealth,
    SessionIndexEntry,
)


def catalog_entry_from_snapshot(
    snapshot: RuntimeSessionSnapshot,
    *,
    resource_count: int = 0,
    resource_missing: bool = False,
) -> SessionIndexEntry:
    session = snapshot.session
    latest_run = snapshot.runs[-1] if snapshot.runs else None
    status = _catalog_status(session, latest_run)
    health: SessionHealth = "recoverable" if status == "recoverable" else "ok"
    if resource_missing:
        health = "missing_resource"
    last_activity_at = session.last_activity_at
    if snapshot.messages:
        last_activity_at = max(last_activity_at, snapshot.messages[-1].created_at)
    if latest_run is not None:
        last_activity_at = max(
            last_activity_at,
            latest_run.ended_at or latest_run.started_at,
        )
    return catalog_entry_from_session(
        session,
        status=status,
        health=health,
        active_run_id=_active_run_id(latest_run),
        message_count=len(snapshot.messages),
        run_count=len(snapshot.runs),
        resource_count=resource_count,
        last_message_preview=_last_message_preview(snapshot.messages),
        search_text=_message_search_text(snapshot.messages),
        last_activity_at=last_activity_at,
    )


def catalog_entry_from_session(
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


def sort_catalog_entries(entries: list[SessionIndexEntry]) -> list[SessionIndexEntry]:
    return sorted(
        entries,
        key=lambda entry: (entry.last_activity_at, entry.session_id),
        reverse=True,
    )


def _catalog_status(
    session: SessionRecord,
    latest_run: RunRecord | None,
) -> SessionCatalogStatus:
    if latest_run is not None:
        if latest_run.status in {"queued", "running"}:
            return "active"
        if latest_run.status == "waiting_approval":
            return "waiting_approval"
        if latest_run.status == "waiting_user":
            return "waiting_user"
        if latest_run.status == "recoverable":
            return "recoverable"
        if latest_run.status == "paused":
            return "paused"
        if latest_run.status == "failed" and latest_run.kind == "agent":
            return "failed"
        return "completed"
    if session.status == "failed":
        return "failed"
    if session.message_count > 0:
        return "completed"
    return "idle"


def _active_run_id(latest_run: RunRecord | None) -> str | None:
    if latest_run is not None and latest_run.status in ACTIVE_RUN_STATUSES:
        return latest_run.run_id
    return None


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
