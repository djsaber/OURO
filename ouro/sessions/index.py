from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

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
PendingInteraction = Literal["approval", "user"]


class SessionIndexEntry(OuroModel):
    """Rebuildable catalog projection for one canonical session."""

    session_id: str
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
    health: SessionHealth = "ok"


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
