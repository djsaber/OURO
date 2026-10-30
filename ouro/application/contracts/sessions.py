from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from ouro.application.contracts.base import ApplicationDTO, Diagnostic
from ouro.application.contracts.events import RuntimeEventView
from ouro.application.contracts.tool_activity import PendingInteraction as RuntimePendingInteraction
from ouro.application.contracts.tool_activity import RuntimeToolCallActivity
from ouro.core.approvals import SessionApprovalMode
from ouro.core.compaction import CompactionRecord, CompactionTrigger
from ouro.core.context import ContextMetrics, ProviderContext
from ouro.core.context_snapshots import ModelRequestHeader
from ouro.core.events import EventRecord
from ouro.core.harness import HarnessVersion
from ouro.core.input_content import InputFile, InputSkillReference
from ouro.core.inputs import InputState
from ouro.core.interactions import SessionInteractionMode
from ouro.core.messages import MessageRecord, ModelMessage
from ouro.core.reasoning import ReasoningRequest
from ouro.core.session_controls import SessionInputMode
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.session_observation import SessionObservationPosition
from ouro.core.state import (
    RunRecord,
    SessionRecord,
    SessionStatus,
    SessionTitleSource,
)
from ouro.core.tool_facts import ToolCallIdentity
from ouro.sessions.index import (
    UNREADABLE_SESSION_HEALTHS,
    PendingInteraction,
    SessionHealth,
    SessionIndexEntry,
)
from ouro.sessions.index import (
    SessionCatalogStatus as SessionCatalogStatus,
)

SessionSort = Literal["updated_desc", "created_desc", "title"]


class SessionUndoResult(ApplicationDTO):
    session: SessionRecord
    input: ModelMessage
    resource_refs: tuple[str, ...] = ()
    attachments: tuple[InputFile, ...] = ()
    message_id: str | None = None
    queue_id: str | None = None
    run_id: str | None = None
    removed_count: int = 0
    cancelled_queue_ids: tuple[str, ...] = ()

    @property
    def text_content(self) -> str:
        return self.input.text_content


class SessionCatalogQuery(ApplicationDTO):
    namespace: str | None = None
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
    namespace: str = "default"
    session_kind: Literal["primary", "subagent"] | None = None
    root_session_id: str
    parent_session_id: str | None = None
    title: str
    title_source: SessionTitleSource = "fallback"
    title_locked: bool = False
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
    workspace_trusted: bool = False
    archived: bool = False
    pinned: bool = False
    tags: tuple[str, ...] = ()
    message_count: int = 0
    run_count: int = 0
    resource_count: int = 0
    last_message_preview: str = ""
    health: SessionHealth | None = "ok"
    error_detail: str | None = None
    catalog_error: str | None = None

    @property
    def catalog_marks_unreadable(self) -> bool:
        return self.catalog_error is None and self.health in UNREADABLE_SESSION_HEALTHS

    @classmethod
    def from_index(cls, entry: SessionIndexEntry) -> SessionCatalogEntry:
        return cls.model_validate(entry.model_dump())


class SessionView(ApplicationDTO):
    history_epoch: int = 0
    harness_version: str | None = None
    session_id: str
    namespace: str = "default"
    session_kind: Literal["primary", "subagent"] = "primary"
    root_session_id: str
    parent_session_id: str | None = None
    delegation_id: str | None = None
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
    reasoning_request: ReasoningRequest = Field(default_factory=ReasoningRequest)
    approval_mode: SessionApprovalMode = "inherit"
    interaction_mode: SessionInteractionMode = "interactive"
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
            history_epoch=session.history_epoch,
            harness_version=session.harness_version,
            session_id=session.session_id,
            namespace=session.namespace,
            session_kind=session.session_kind,
            root_session_id=session.root_session_id or session.session_id,
            parent_session_id=session.parent_session_id,
            delegation_id=session.delegation_id,
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
            reasoning_request=session.reasoning_request,
            approval_mode=session.approval_mode,
            interaction_mode=session.interaction_mode,
            active_compaction_id=session.context_selection.compaction_id,
            status=session.status,
            active_run_id=session.active_run_id,
            last_message_id=session.last_message_id,
            last_run_id=session.last_run_id,
            message_count=session.message_count,
            tags=tuple(session.tags),
        )

    @property
    def agent_root_session_id(self) -> str:
        """Return the primary Session that owns this Session's Agent tree."""

        return self.session_id if self.session_kind == "primary" else self.root_session_id


class SessionCatalogResult(ApplicationDTO):
    entries: tuple[SessionCatalogEntry, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()


class SessionDetail(ApplicationDTO):
    session: SessionView
    recent_run: RunRecord | None = None
    active_compaction: CompactionRecord | None = None
    diagnostics: tuple[Diagnostic, ...] = ()


class SessionSummary(ApplicationDTO):
    """One persisted compaction summary and its state at the query boundary."""

    session_id: str
    compaction_id: str
    created_at: datetime
    trigger: CompactionTrigger
    summary: str
    active: bool


class SessionContextStatus(ApplicationDTO):
    context_id: str
    session_id: str
    run_id: str
    turn_id: str
    model_round: int
    provider_id: str
    endpoint_id: str | None = None
    model_id: str
    prepared_at: datetime
    completed_at: datetime | None = None
    interrupted_at: datetime | None = None
    usage_entry_id: str | None = None
    metrics: ContextMetrics
    provider_reported_input_tokens: int | None = None
    active_compaction_id: str | None = None

    @classmethod
    def from_record(
        cls, record: ModelRequestHeader, *, reported_input_tokens: int | None = None
    ) -> SessionContextStatus:
        compaction_id = record.context_metadata.get("compaction_id")
        return cls(
            context_id=record.context_id,
            session_id=record.session_id,
            run_id=record.run_id,
            turn_id=record.turn_id,
            model_round=record.model_round,
            provider_id=record.provider_id,
            endpoint_id=record.endpoint_id,
            model_id=record.model_id,
            prepared_at=record.prepared_at,
            completed_at=record.provider_done_at,
            interrupted_at=record.interrupted_at,
            usage_entry_id=record.usage_entry_id,
            metrics=record.metrics,
            provider_reported_input_tokens=reported_input_tokens,
            active_compaction_id=compaction_id if isinstance(compaction_id, str) else None,
        )


class SessionContextSnapshot(ApplicationDTO):
    status: SessionContextStatus
    provider_context: ProviderContext


class SessionExport(ApplicationDTO):
    """Explicit complete-history transfer DTO; interactive Host reads stay paged."""

    session: SessionRecord
    messages: tuple[MessageRecord, ...] = ()
    runs: tuple[RunRecord, ...] = ()
    compactions: tuple[CompactionRecord, ...] = ()
    extension_states: tuple[SessionExtensionStateRecord, ...] = ()
    harness_versions: tuple[HarnessVersion, ...] = ()


class SessionMessagePage(ApplicationDTO):
    session_id: str
    history_epoch: int = 0
    messages: tuple[MessageRecord, ...] = ()
    next_cursor: int | None = None


class SessionHistoryAnchor(ApplicationDTO):
    """The first retained canonical message in one history epoch."""

    history_epoch: int = Field(ge=0)
    first_sequence: int = Field(ge=1)


class SessionHistoryWindow(ApplicationDTO):
    """A history page with complete model turns and their presentation records."""

    session_id: str
    history_epoch: int
    messages: tuple[MessageRecord, ...] = ()
    runs: tuple[RunRecord, ...] = ()
    compactions: tuple[CompactionRecord, ...] = ()
    before_cursor: int | None = None


class QueuedUserInputView(ApplicationDTO):
    """One pending input and the Run it currently awaits, read from the same Session.

    Without an active Run, the input is ready for dispatch. This is independent
    of a Supervisor's available execution capacity and the Host's presentation.
    """

    queue_id: str
    prompt: str
    mode: SessionInputMode
    queued_at: datetime
    waiting_on_run_id: str | None = None
    skills: tuple[InputSkillReference, ...] = ()
    attachments: tuple[InputFile, ...] = Field(default=(), max_length=16)
    resource_refs: tuple[str, ...] = ()


class SessionObservationSnapshot(ApplicationDTO):
    """One atomic durable boundary from which a Host can attach a live stream."""

    session: SessionView
    messages: tuple[MessageRecord, ...] = ()
    events: tuple[EventRecord, ...] = ()
    runs: tuple[RunRecord, ...] = ()
    active_tool_calls: tuple[RuntimeToolCallActivity, ...] = ()
    pending_interactions: tuple[RuntimePendingInteraction, ...] = ()
    current_request: ModelRequestHeader | None = None
    compactions: tuple[CompactionRecord, ...] = ()
    queued_inputs: tuple[QueuedUserInputView, ...] = ()
    position: SessionObservationPosition = Field(default_factory=SessionObservationPosition)
    context_status: SessionContextStatus | None = None
    effective_context_metrics: ContextMetrics | None = None
    history_before_cursor: int | None = None


class SessionObservationFacts(ApplicationDTO):
    """Current changed records; messages and drafts travel with the same page."""

    session: SessionView
    runs: tuple[RunRecord, ...] = ()
    run_ends: dict[str, int] = Field(default_factory=dict)
    changed_calls: tuple[ToolCallIdentity, ...] = ()
    tool_calls: tuple[RuntimeToolCallActivity, ...] = ()
    pending_interactions: tuple[RuntimePendingInteraction, ...] = ()
    inputs: tuple[InputState, ...] = ()
    queued_inputs: tuple[QueuedUserInputView, ...] = ()
    compactions: tuple[CompactionRecord, ...] = ()
    compaction_ends: dict[str, int] = Field(default_factory=dict)
    current_request: ModelRequestHeader | None = None
    context_status: SessionContextStatus | None = None
    effective_context_metrics: ContextMetrics | None = None


class SessionObservationBatch(ApplicationDTO):
    """Display facts awaiting content application by one observation consumer."""

    session_id: str
    history_epoch: int
    snapshot: SessionObservationSnapshot | None = None
    events: tuple[RuntimeEventView, ...] = ()
    messages: tuple[MessageRecord, ...] = ()
    facts: SessionObservationFacts | None = None
    after: SessionObservationPosition = Field(default_factory=SessionObservationPosition)
    position: SessionObservationPosition = Field(default_factory=SessionObservationPosition)
    diagnostic: Diagnostic | None = None


class SessionMutationResult(ApplicationDTO):
    session: SessionView
    diagnostics: tuple[Diagnostic, ...] = ()


class SessionBranchResult(ApplicationDTO):
    session: SessionView
    source_session_id: str
    source_message_id: str | None = None
    message_id_map: dict[str, str] = Field(default_factory=dict)
    diagnostics: tuple[Diagnostic, ...] = ()


class SessionTrashResult(ApplicationDTO):
    session_id: str
    trash_id: str
    session_ids: tuple[str, ...]
    diagnostics: tuple[Diagnostic, ...] = ()
