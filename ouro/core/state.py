from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from ouro.core.approvals import SessionApprovalMode
from ouro.core.base import SCHEMA_VERSION, Metadata, MetadataField, OuroModel, new_id, utc_now
from ouro.core.tool_batches import ToolCallBatchRecord

RunStatus = Literal[
    "queued",
    "running",
    "waiting_user",
    "waiting_approval",
    "completed",
    "aborted",
    "failed",
    "paused",
    "recoverable",
]
ACTIVE_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {"queued", "running", "waiting_user", "waiting_approval"}
)
RunKind = Literal["agent", "compaction"]
RunOrigin = Literal["user", "follow_up", "replay", "plugin"]

SessionStatus = Literal["idle", "running", "waiting_user", "waiting_approval", "failed"]
SessionTitleSource = Literal["fallback", "generated", "user"]
RunPhase = Literal[
    "model_streaming", "tool_preflight", "human_interaction", "tool_execution", "result_commit"
]


def session_status_for_run(status: RunStatus) -> SessionStatus:
    """Project the authoritative active Run status onto its owning Session."""

    if status == "waiting_approval":
        return "waiting_approval"
    if status == "waiting_user":
        return "waiting_user"
    if status == "failed":
        return "failed"
    return "running"


class SessionRecord(OuroModel):
    schema_version: int = SCHEMA_VERSION
    session_id: str = Field(default_factory=lambda: new_id("ses"))
    root_session_id: str | None = None
    parent_session_id: str | None = None
    forked_from_message_id: str | None = None
    forked_from_run_id: str | None = None
    title: str = "New session"
    title_source: SessionTitleSource = "fallback"
    title_locked: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_activity_at: datetime = Field(default_factory=utc_now)
    archived: bool = False
    pinned: bool = False
    workspace: str | None = None
    workspace_trusted: bool = False
    current_model: str | None = None
    approval_mode: SessionApprovalMode = "inherit"
    active_compaction_id: str | None = None
    status: SessionStatus = "idle"
    active_run_id: str | None = None
    last_message_id: str | None = None
    last_run_id: str | None = None
    message_count: int = 0
    tags: list[str] = Field(default_factory=list)
    metadata: Metadata = MetadataField

    @model_validator(mode="after")
    def default_root_session_id(self) -> Self:
        if self.root_session_id is None:
            self.root_session_id = self.session_id
        return self


class RunRecord(OuroModel):
    schema_version: int = SCHEMA_VERSION
    run_id: str = Field(default_factory=lambda: new_id("run"))
    session_id: str
    kind: RunKind = "agent"
    origin: RunOrigin = "user"
    parent_run_id: str | None = None
    initiator_id: str | None = None
    status: RunStatus = "queued"
    phase: RunPhase = "model_streaming"
    trigger: str = "user"
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    model: str | None = None
    pending_approval_id: str | None = None
    active_tool_batch: ToolCallBatchRecord | None = None
    abort_requested: bool = False
    error: str | None = None
    usage: dict[str, int | float] = Field(default_factory=dict)
    metadata: Metadata = MetadataField
