from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from ouro.core.agents import AgentCatalogSnapshot
from ouro.core.approvals import SessionApprovalMode
from ouro.core.base import Metadata, MetadataField, OuroModel, VersionedOuroModel, new_id, utc_now
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.core.interactions import SessionInteractionMode
from ouro.core.message_semantics import SemanticTag
from ouro.core.reasoning import ReasoningRequest
from ouro.core.session_controls import (
    RunControlBinding,
    SessionControlState,
    SessionControlTransitionRequest,
    SessionInputMode,
    SessionInputRole,
)
from ouro.core.tool_batches import ToolCallBatchRecord

RunStatus = Literal[
    "queued",
    "running",
    "waiting_user",
    "waiting_approval",
    "waiting_child",
    "completed",
    "aborted",
    "failed",
    "paused",
    "recoverable",
]
ACTIVE_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {"queued", "running", "waiting_user", "waiting_approval", "waiting_child"}
)
RESUMABLE_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {"aborted", "failed", "paused", "recoverable"}
)
RunKind = Literal["agent", "compaction"]
RunOrigin = Literal["user", "follow_up", "replay", "host", "plugin", "delegation"]

SessionStatus = Literal["idle", "running", "waiting_user", "waiting_approval", "failed"]
SessionTitleSource = Literal["fallback", "generated", "user"]
RunPhase = Literal[
    "model_streaming",
    "tool_preflight",
    "human_interaction",
    "tool_execution",
    "subagent_join",
    "result_commit",
]
RunContinuationState = Literal["pending", "resolved"]


class RunOutcome(OuroModel):
    """Bounded terminal Run projection shared by extension control surfaces."""

    harness: HarnessCallSnapshot | None = None
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
    active_when_controller_id: str | None = None
    activation_id: str | None = None

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
        if (self.active_when_controller_id is None) is not (self.activation_id is None):
            raise ValueError(
                "activation-bound continuation requires both controller and activation ids"
            )
        return self

    @property
    def canonical_id(self) -> str:
        return f"{self.plugin_id}/continuation_policies/{self.policy_id}"


class RunContinuationSettlementPlan(OuroModel):
    """Durable policy decision staged without process-local generation identity."""

    plugin_id: str
    policy_id: str
    execution_contract_hash: str
    contract_hash: str
    action: Literal["continue", "complete", "pause", "fail"]
    prompt: str | None = None
    mode: SessionInputMode = "follow_up"
    title: str | None = None
    role: SessionInputRole = "system"
    scaffold: bool = True
    semantic_tag: SemanticTag | None = None
    coalesce_key: str | None = Field(default=None, min_length=1, max_length=128)
    reason: str | None = None
    state_revision: int | None = Field(default=None, ge=0)
    session_control_transitions: list[SessionControlTransitionRequest] = Field(default_factory=list)
    harness: HarnessCallSnapshot | None = None
    released_activation_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_action_payload(self) -> Self:
        if self.action == "continue":
            if self.prompt is None or not self.prompt.strip():
                raise ValueError("continue settlement plan requires a non-empty prompt")
            if self.session_control_transitions:
                raise ValueError(
                    "continue settlement plan cannot contain Session control transitions"
                )
        elif self.prompt is not None:
            raise ValueError(f"{self.action} settlement plan cannot contain a prompt")
        return self


class RunContinuationCheckpoint(OuroModel):
    decision_id: str = Field(default_factory=lambda: new_id("decision"))
    queue_id: str = Field(default_factory=lambda: new_id("queued"))
    state: RunContinuationState = "pending"
    controllers: list[RunContinuationControllerRef] = Field(default_factory=list)
    settlement_plan: RunContinuationSettlementPlan | None = None
    action: Literal["continue", "complete", "pause", "fail"] | None = None
    selected_controller_id: str | None = None
    reason: str | None = None
    state_revision: int | None = None
    resolved_at: datetime | None = None


class SessionRecord(VersionedOuroModel):
    schema_version: Literal[2] = 2
    session_id: str = Field(default_factory=lambda: new_id("ses"))
    harness_version: str = Field(
        default_factory=lambda: new_id("harness"), pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
    )
    history_epoch: int = Field(default=0, ge=0)
    namespace: str = Field(default="default", min_length=1, max_length=128)
    root_session_id: str | None = None
    parent_session_id: str | None = None
    session_kind: Literal["primary", "subagent"] = "primary"
    delegation_id: str | None = None
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
    reasoning_request: ReasoningRequest = Field(default_factory=ReasoningRequest)
    approval_mode: SessionApprovalMode = "inherit"
    interaction_mode: SessionInteractionMode = "interactive"
    controls: SessionControlState = Field(default_factory=SessionControlState)
    active_compaction_id: str | None = None
    status: SessionStatus = "idle"
    active_run_id: str | None = None
    last_message_id: str | None = None
    last_run_id: str | None = None
    message_count: int = 0
    tags: list[str] = Field(default_factory=list)
    metadata: Metadata = MetadataField

    @model_validator(mode="after")
    def validate_session_identity(self) -> Self:
        if self.root_session_id is None:
            self.root_session_id = self.session_id
        if self.session_kind == "primary":
            if self.delegation_id is not None:
                raise ValueError("primary session cannot carry delegation_id")
        elif (
            self.delegation_id is None
            or self.parent_session_id is None
            or self.root_session_id == self.session_id
        ):
            raise ValueError(
                "subagent session requires distinct root, parent, and delegation identity"
            )
        return self

    @property
    def agent_root_session_id(self) -> str:
        """Return the durable Agent aggregate owned by this Session.

        Primary forks retain their lineage in ``root_session_id``, but each
        primary Session owns an independent Agent tree. Subagent Sessions keep
        pointing at the primary Session that owns their Delegation records.
        """

        if self.session_kind == "primary":
            return self.session_id
        assert self.root_session_id is not None
        return self.root_session_id


class RunRecord(VersionedOuroModel):
    schema_version: Literal[2] = 2
    history_epoch: int = Field(default=0, ge=0)
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
    model_endpoint_id: str | None = None
    pending_approval_id: str | None = None
    active_tool_batch: ToolCallBatchRecord | None = None
    abort_requested: bool = False
    error: str | None = None
    usage: dict[str, int | float] = Field(default_factory=dict)
    usage_entry_ids: list[str] = Field(default_factory=list)
    model_round_count: int = Field(default=0, ge=0)
    continuation: RunContinuationCheckpoint | None = None
    agent_catalog: AgentCatalogSnapshot | None = None
    metadata: Metadata = MetadataField

    @property
    def control_bindings(self) -> tuple[RunControlBinding, ...]:
        return tuple(
            RunControlBinding.model_validate(item)
            for item in self.metadata.get("session_control_snapshot", {}).get("activations", ())
        )
