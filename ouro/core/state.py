from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

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

SessionStatus = Literal["idle", "running", "waiting_user", "waiting_approval", "failed"]
ApprovalStatus = Literal["requested", "approved", "rejected", "modified", "expired", "aborted"]


class SessionRecord(OuroModel):
    schema_version: int = SCHEMA_VERSION
    session_id: str = Field(default_factory=lambda: new_id("ses"))
    title: str = "New session"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    archived: bool = False
    workspace: str | None = None
    current_model: str | None = None
    active_compaction_id: str | None = None
    status: SessionStatus = "idle"
    last_message_id: str | None = None
    last_run_id: str | None = None
    message_count: int = 0
    tags: list[str] = Field(default_factory=list)
    metadata: Metadata = MetadataField


class RunRecord(OuroModel):
    schema_version: int = SCHEMA_VERSION
    run_id: str = Field(default_factory=lambda: new_id("run"))
    session_id: str
    kind: RunKind = "agent"
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
