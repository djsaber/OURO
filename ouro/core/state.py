from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from ouro.core.approvals import SessionApprovalMode
from ouro.core.base import SCHEMA_VERSION, Metadata, MetadataField, OuroModel, new_id, utc_now
from ouro.core.interactions import SessionInteractionMode
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
RunOrigin = Literal["user", "follow_up", "replay", "host", "plugin"]

SessionStatus = Literal["idle", "running", "waiting_user", "waiting_approval", "failed"]
SessionTitleSource = Literal["fallback", "generated", "user"]
RunPhase = Literal[
    "model_streaming", "tool_preflight", "human_interaction", "tool_execution", "result_commit"
]
RunContinuationState = Literal["pending", "resolved"]


class RunOutcome(OuroModel):
    """Bounded terminal Run projection shared by extension control surfaces."""

    session_id: str
    run_id: str
    status: RunStatus
    origin: RunOrigin
    final_text: str = ""
    error: str | None = None
    usage: dict[str, int | float] = Field(default_factory=dict)


class RunContinuationControllerRef(OuroModel):
    plugin_id: str
    policy_id: str
    version: str
    plugin_class: Literal["builtin", "user"] = "user"
    release_id: str | None = None
    package_digest: str | None = None
    builtin_contract_hash: str | None = None
    generation_id: str
    contract_hash: str
    execution_contract_hash: str

    @model_validator(mode="after")
    def validate_release_identity(self) -> Self:
        if self.plugin_class == "user":
            if self.package_digest is None or self.builtin_contract_hash is not None:
                raise ValueError("user continuation controller requires only package_digest")
            if self.release_id is not None and self.release_id != self.package_digest:
                raise ValueError(
                    "user continuation controller release id must equal package digest"
                )
        elif (
            self.release_id is None
            or self.builtin_contract_hash is None
            or self.package_digest is not None
        ):
            raise ValueError(
                "builtin continuation controller requires release_id and builtin_contract_hash"
            )
        elif self.release_id != self.builtin_contract_hash:
            raise ValueError("builtin continuation controller release id must equal contract hash")
        return self

    @property
    def canonical_id(self) -> str:
        return f"{self.plugin_id}/continuation_policies/{self.policy_id}"


class RunContinuationCheckpoint(OuroModel):
    decision_id: str = Field(default_factory=lambda: new_id("decision"))
    queue_id: str = Field(default_factory=lambda: new_id("queued"))
    state: RunContinuationState = "pending"
    controllers: list[RunContinuationControllerRef] = Field(default_factory=list)
    action: Literal["continue", "complete", "pause", "fail"] | None = None
    selected_controller_id: str | None = None
    reason: str | None = None
    state_revision: int | None = None
    resolved_at: datetime | None = None


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
    interaction_mode: SessionInteractionMode = "interactive"
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
    usage_entry_ids: list[str] = Field(default_factory=list)
    continuation: RunContinuationCheckpoint | None = None
    metadata: Metadata = MetadataField
