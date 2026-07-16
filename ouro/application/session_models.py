from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from ouro.application.dto import ApplicationDTO, Diagnostic
from ouro.core.approvals import SessionApprovalMode
from ouro.core.compaction import CompactionRecord
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.state import (
    RunRecord,
    SessionRecord,
    SessionStatus,
    SessionTitleSource,
)
from ouro.sessions.index import (
    PendingInteraction,
    SessionHealth,
    SessionIndexEntry,
)
from ouro.sessions.index import (
    SessionCatalogStatus as SessionCatalogStatus,
)

SessionSort = Literal["updated_desc", "created_desc", "title"]


class SessionCatalogQuery(ApplicationDTO):
    text: str | None = None
    statuses: frozenset[SessionCatalogStatus] = Field(default_factory=frozenset)
    archived: bool | None = None
    pinned: bool | None = None
    workspace: str | None = None
    model: str | None = None
    tags: frozenset[str] = Field(default_factory=frozenset)
    created_after: datetime | None = None
    created_before: datetime | None = None
    updated_after: datetime | None = None
    updated_before: datetime | None = None
    health: frozenset[SessionHealth] = Field(default_factory=frozenset)
    sort: SessionSort = "updated_desc"


class SessionCatalogEntry(ApplicationDTO):
    session_id: str
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
    workspace: str | None = None
    workspace_trusted: bool = False
    archived: bool = False
    pinned: bool = False
    tags: tuple[str, ...] = ()
    message_count: int = 0
    run_count: int = 0
    resource_count: int = 0
    last_message_preview: str = ""
    health: SessionHealth = "ok"

    @classmethod
    def from_index(cls, entry: SessionIndexEntry) -> SessionCatalogEntry:
        return cls.model_validate(entry.model_dump(exclude={"search_text"}))


class SessionView(ApplicationDTO):
    session_id: str
    root_session_id: str
    parent_session_id: str | None = None
    forked_from_message_id: str | None = None
    forked_from_run_id: str | None = None
    title: str
    title_source: SessionTitleSource
    title_locked: bool
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    archived: bool
    pinned: bool
    workspace: str | None = None
    workspace_trusted: bool
    current_model: str | None = None
    approval_mode: SessionApprovalMode = "inherit"
    active_compaction_id: str | None = None
    status: SessionStatus
    active_run_id: str | None = None
    last_message_id: str | None = None
    last_run_id: str | None = None
    message_count: int
    tags: tuple[str, ...] = ()

    @classmethod
    def from_record(cls, session: SessionRecord) -> SessionView:
        return cls(
            session_id=session.session_id,
            root_session_id=session.root_session_id or session.session_id,
            parent_session_id=session.parent_session_id,
            forked_from_message_id=session.forked_from_message_id,
            forked_from_run_id=session.forked_from_run_id,
            title=session.title,
            title_source=session.title_source,
            title_locked=session.title_locked,
            created_at=session.created_at,
            updated_at=session.updated_at,
            last_activity_at=session.last_activity_at,
            archived=session.archived,
            pinned=session.pinned,
            workspace=session.workspace,
            workspace_trusted=session.workspace_trusted,
            current_model=session.current_model,
            approval_mode=session.approval_mode,
            active_compaction_id=session.active_compaction_id,
            status=session.status,
            active_run_id=session.active_run_id,
            last_message_id=session.last_message_id,
            last_run_id=session.last_run_id,
            message_count=session.message_count,
            tags=tuple(session.tags),
        )


class SessionCatalogResult(ApplicationDTO):
    entries: tuple[SessionCatalogEntry, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()


class SessionDetail(ApplicationDTO):
    session: SessionView
    recent_run: RunRecord | None = None
    active_compaction: CompactionRecord | None = None
    diagnostics: tuple[Diagnostic, ...] = ()


class SessionExport(ApplicationDTO):
    """Explicit complete-history transfer DTO; interactive Host reads stay paged."""

    session: SessionView
    messages: tuple[MessageRecord, ...] = ()
    events: tuple[EventRecord, ...] = ()
    runs: tuple[RunRecord, ...] = ()
    compactions: tuple[CompactionRecord, ...] = ()


class SessionMessagePage(ApplicationDTO):
    session_id: str
    messages: tuple[MessageRecord, ...] = ()
    next_cursor: int | None = None


class SessionEventPage(ApplicationDTO):
    session_id: str
    events: tuple[EventRecord, ...] = ()
    next_cursor: int | None = None


class SessionMutationResult(ApplicationDTO):
    session: SessionView
    diagnostics: tuple[Diagnostic, ...] = ()


class SessionBranchResult(ApplicationDTO):
    session: SessionView
    source_session_id: str
    source_message_id: str
    message_id_map: dict[str, str] = Field(default_factory=dict)
    diagnostics: tuple[Diagnostic, ...] = ()


class ReplayFrame(ApplicationDTO):
    sequence: int
    event: EventRecord


class SessionReplay(ApplicationDTO):
    session_id: str
    run_id: str | None = None
    frames: tuple[ReplayFrame, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()


class SessionTrashResult(ApplicationDTO):
    session_id: str
    trash_id: str
    diagnostics: tuple[Diagnostic, ...] = ()
