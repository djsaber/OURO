from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from ouro.core.approvals import SessionApprovalMode
from ouro.core.base import OuroModel, new_id, utc_now
from ouro.core.interactions import SessionInteractionMode
from ouro.sessions.layout import DATA_LAYOUT_VERSION

SessionCatalogStatus = Literal[
    "idle",
    "active",
    "waiting_approval",
    "waiting_user",
    "failed",
    "paused",
    "recoverable",
    "completed",
]
SessionHealth = Literal[
    "ok",
    "recoverable",
    "corrupt",
    "missing_resource",
    "migration_required",
]
UNREADABLE_SESSION_HEALTHS: frozenset[SessionHealth] = frozenset(
    {"corrupt", "migration_required"}
)
PendingInteraction = Literal["approval", "user"]


class SessionIndexEntry(OuroModel):
    """Rebuildable catalog projection for one canonical session."""

    session_id: str
    namespace: str = "default"
    session_kind: Literal["primary", "subagent"] | None = None
    root_session_id: str
    parent_session_id: str | None = None
    title: str
    title_source: Literal["fallback", "generated", "user"] = "fallback"
    title_locked: bool = False
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    status: SessionCatalogStatus
    active_run_id: str | None = None
    pending_interaction: PendingInteraction | None = None
    current_provider: str | None = None
    current_endpoint: str | None = None
    current_model: str | None = None
    approval_mode: SessionApprovalMode = "inherit"
    interaction_mode: SessionInteractionMode = "interactive"
    workspace: str | None = None
    workspace_trusted: bool = False
    archived: bool = False
    pinned: bool = False
    tags: list[str] = Field(default_factory=list)
    message_count: int = 0
    run_count: int = 0
    resource_count: int = 0
    last_message_preview: str = ""
    health: SessionHealth | None = "ok"
    error_detail: str | None = None
    catalog_error: str | None = None

    @model_validator(mode="after")
    def require_complete_valid_projection(self) -> SessionIndexEntry:
        if self.catalog_error is not None:
            if self.health is not None:
                raise ValueError("an invalid catalog projection cannot claim Session health")
            return self
        if self.health is None:
            raise ValueError("a valid catalog projection requires Session health")
        if self.session_kind is None and self.health not in UNREADABLE_SESSION_HEALTHS:
            raise ValueError("a readable catalog projection requires session_kind")
        return self


def invalid_session_index_entry(
    payload: object,
    *,
    error: str,
    fallback_session_id: str,
    health: SessionHealth = "corrupt",
    error_scope: Literal["canonical", "catalog"] = "canonical",
) -> SessionIndexEntry:
    """Retain readable identity while keeping catalog and canonical errors distinct."""

    if isinstance(payload, dict):
        raw_session_kind = payload.get("session_kind")
        session_kind = raw_session_kind if raw_session_kind in {"primary", "subagent"} else None
        candidate = {
            **payload,
            "session_kind": session_kind,
            "health": None if error_scope == "catalog" else health,
            "error_detail": error if error_scope == "canonical" else None,
            "catalog_error": error if error_scope == "catalog" else None,
        }
        if error_scope == "canonical":
            candidate["status"] = "failed"
        try:
            return SessionIndexEntry.model_validate(candidate)
        except ValidationError:
            session_id = payload.get("session_id")
            title = payload.get("title")
    else:
        session_id = None
        title = None
        session_kind = None
    now = utc_now()
    identity = session_id if isinstance(session_id, str) and session_id else fallback_session_id
    return SessionIndexEntry(
        session_id=identity,
        session_kind=session_kind,
        root_session_id=identity,
        title=(
            title
            if isinstance(title, str) and title
            else "Invalid session index entry"
            if error_scope == "catalog"
            else "Migration required"
            if health == "migration_required"
            else "Corrupt session"
        ),
        created_at=now,
        updated_at=now,
        last_activity_at=now,
        status="failed",
        health=None if error_scope == "catalog" else health,
        error_detail=error if error_scope == "canonical" else None,
        catalog_error=error if error_scope == "catalog" else None,
    )


class SessionIndex(OuroModel):
    """Versioned, disposable index document stored under ``sessions/index.json``."""

    data_layout_version: int = DATA_LAYOUT_VERSION
    generation: str = Field(default_factory=lambda: new_id("idx"))
    generated_at: datetime = Field(default_factory=utc_now)
    sessions: list[SessionIndexEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_current_layout(self) -> SessionIndex:
        if self.data_layout_version != DATA_LAYOUT_VERSION:
            raise ValueError(
                "session index layout version "
                f"{self.data_layout_version} is not supported; expected {DATA_LAYOUT_VERSION}"
            )
        return self
