from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field, model_validator

from ouro.core.approvals import SessionApprovalMode
from ouro.core.base import OuroModel
from ouro.core.interactions import SessionInteractionMode
from ouro.core.state import SessionTitleSource

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
    "unsupported_version",
]
UNREADABLE_SESSION_HEALTHS: frozenset[SessionHealth] = frozenset({"corrupt", "unsupported_version"})
PendingInteraction = Literal["approval", "user"]


class SessionIndexEntry(OuroModel):
    """Rebuildable catalog projection for one canonical session."""

    session_id: str
    namespace: str = "default"
    session_kind: Literal["primary", "subagent"] | None = None
    root_session_id: str
    parent_session_id: str | None = None
    title: str
    title_source: SessionTitleSource = "fallback"
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    status: SessionCatalogStatus
    active_run_id: str | None = None
    pending_interaction: PendingInteraction | None = None
    current_model: str | None = None
    approval_mode: SessionApprovalMode = "inherit"
    interaction_mode: SessionInteractionMode = "interactive"
    workspace: str | None = None
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


class SessionCatalog(Protocol):
    """Read projection, independent of the index document's storage metadata."""

    @property
    def sessions(self) -> list[SessionIndexEntry]: ...
