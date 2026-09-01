from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ouro.core.messages import MessageRecord
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord, SessionRecord
from ouro.sessions.index import (
    PendingInteraction,
    SessionCatalogStatus,
    SessionHealth,
    SessionIndexEntry,
)


@dataclass(frozen=True)
class SessionCatalogProjection:
    latest_run: RunRecord | None = None
    latest_message: MessageRecord | None = None
    message_count: int = 0
    run_count: int = 0
    resource_count: int = 0
    resource_missing: bool = False


def catalog_entry_from_projection(
    session: SessionRecord,
    projection: SessionCatalogProjection,
) -> SessionIndexEntry:
    latest_run = projection.latest_run
    status = _catalog_status(session, projection.latest_run)
    health: SessionHealth = "recoverable" if status == "recoverable" else "ok"
    if projection.resource_missing:
        health = "missing_resource"
    return catalog_entry_from_session(
        session,
        status=status,
        health=health,
        active_run_id=_active_run_id(latest_run),
        message_count=projection.message_count,
        run_count=projection.run_count,
        resource_count=projection.resource_count,
        last_message_preview=message_preview(projection.latest_message),
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
        session_kind=session.session_kind,
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
        interaction_mode=session.interaction_mode,
        workspace=session.workspace,
        workspace_trusted=session.workspace_trusted,
        archived=session.archived,
        pinned=session.pinned,
        tags=list(session.tags),
        message_count=session.message_count if message_count is None else message_count,
        run_count=run_count,
        resource_count=resource_count,
        last_message_preview=last_message_preview,
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
        if latest_run.status in {"queued", "running", "waiting_child"}:
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


def message_preview(message: MessageRecord | None, *, limit: int = 160) -> str:
    if message is None or message.role not in {"user", "assistant"}:
        return ""
    text = " ".join(message.text_content.split())
    return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"
