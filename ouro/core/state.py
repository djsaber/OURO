from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from ouro.core.base import SCHEMA_VERSION, Metadata, MetadataField, OuroModel, new_id, utc_now

RunStatus = Literal[
    "queued",
    "running",
    "waiting_user",
    "waiting_approval",
    "completed",
    "aborted",
    "failed",
    "recoverable",
]
RunKind = Literal["agent", "compaction"]
RunOrigin = Literal["user", "follow_up", "replay", "plugin"]

SessionStatus = Literal["idle", "running", "waiting_user", "waiting_approval", "failed"]
SessionTitleSource = Literal["fallback", "generated", "user"]
ApprovalStatus = Literal["requested", "approved", "rejected", "modified", "expired", "aborted"]


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
    trigger: str = "user"
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    model: str | None = None
    pending_approval_id: str | None = None
    abort_requested: bool = False
    error: str | None = None
    usage: dict[str, int | float] = Field(default_factory=dict)
    metadata: Metadata = MetadataField


class ApprovalRecord(OuroModel):
    schema_version: int = SCHEMA_VERSION
    approval_id: str = Field(default_factory=lambda: new_id("apv"))
    session_id: str
    run_id: str
    tool_call_id: str
    status: ApprovalStatus = "requested"
    requested_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None
    reason: str | None = None
    modified_arguments: dict[str, object] | None = None
    metadata: Metadata = MetadataField
