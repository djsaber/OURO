from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from ouro.core.approvals import SessionApprovalMode
from ouro.core.base import SCHEMA_VERSION, OuroModel, new_id, utc_now
from ouro.core.interactions import SessionInteractionMode
from ouro.core.run_budget import RunBudgetPolicy

AgentSource = Literal["builtin", "user"]
ToolSelectionMode = Literal["all", "selected"]
ContextMode = Literal["task_only", "summary", "selected_messages", "bounded_history"]
ExecutionMode = Literal["foreground", "background"]
LifecycleBinding = Literal["bound", "detached"]
SpawnState = Literal["requested", "child_created", "submitted", "failed"]
ControlState = Literal["open", "closed"]
DeliveryPolicy = Literal["notify_only", "next_boundary", "follow_up", "resume_waiter"]
DeliveryStatus = Literal["pending", "delivered", "skipped", "failed"]
WaitCompletionCondition = Literal["all", "any"]
WaitState = Literal["pending", "resolved"]
AgentControlAction = Literal["spawn", "wait", "list", "send", "interrupt"]
AgentControlStatus = Literal["completed", "waiting_child"]

_AGENT_ID = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
AGENT_CONTROL_TOOL_NAMES = frozenset(
    {"SpawnAgent", "WaitAgents", "ListAgents", "SendAgentMessage", "InterruptAgent"}
)


def _contract_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class ToolSelection(OuroModel):
    mode: ToolSelectionMode = "all"
    names: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        normalized = tuple(name.strip() for name in self.names)
        if any(not name for name in normalized):
            raise ValueError("tool names must not be empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("selected tool names must be unique")
        if self.mode == "all" and normalized:
            raise ValueError("all tool selection cannot contain explicit names")
        self.names = normalized
        return self


class ContextPolicy(OuroModel):
    mode: ContextMode = "task_only"
    max_messages: int | None = Field(default=None, ge=1, le=200)
    max_tokens: int | None = Field(default=None, ge=1, le=200_000)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.mode == "bounded_history" and (
            self.max_messages is None or self.max_tokens is None
        ):
            raise ValueError("bounded_history requires max_messages and max_tokens")
        if self.mode != "bounded_history" and (
            self.max_messages is not None or self.max_tokens is not None
        ):
            raise ValueError("history bounds are only valid for bounded_history")
        return self


class AgentDefinition(OuroModel):
    schema_version: int = SCHEMA_VERSION
    agent_id: str
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    system_prompt: str = Field(default="", max_length=20_000)
    tool_selection: ToolSelection = Field(default_factory=ToolSelection)
    context_policy: ContextPolicy = Field(default_factory=ContextPolicy)
    model: str | None = None
    can_spawn: bool = False
    default_budget: RunBudgetPolicy = Field(default_factory=RunBudgetPolicy)
    contract_hash: str = ""

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, value: str) -> str:
        if not _AGENT_ID.fullmatch(value):
            raise ValueError("agent_id must be a lowercase stable identifier")
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        expected = self.compute_contract_hash()
        if self.contract_hash and self.contract_hash != expected:
            raise ValueError("agent definition contract hash does not match its content")
        self.contract_hash = expected
        return self

    def compute_contract_hash(self) -> str:
        value = self.model_dump(mode="json", exclude={"contract_hash"})
        return _contract_hash(value)


class EffectiveToolRef(OuroModel):
    public_name: str = Field(min_length=1, max_length=128)
    canonical_tool_id: str = Field(min_length=1, max_length=256)
    owner_id: str | None = Field(default=None, max_length=256)
    execution_contract_hash: str = Field(min_length=1, max_length=256)
    source: Literal[
        "builtin",
        "official_plugin",
        "third_party_plugin",
        "ai_generated_plugin",
        "mcp",
    ] = "builtin"
    effect: Literal["read_only", "state_changing", "unknown"] = "unknown"


class AgentCatalogEntry(OuroModel):
    definition: AgentDefinition
    effective_tools: tuple[EffectiveToolRef, ...] = ()
    missing_tool_names: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_tools(self) -> Self:
        names = [tool.public_name for tool in self.effective_tools]
        if len(set(names)) != len(names):
            raise ValueError("effective tool names must be unique")
        if len(set(self.missing_tool_names)) != len(self.missing_tool_names):
            raise ValueError("missing tool names must be unique")
        return self

    @property
    def available(self) -> bool:
        return not self.missing_tool_names


class AgentCatalogSnapshot(OuroModel):
    schema_version: int = SCHEMA_VERSION
    revision: int = Field(default=0, ge=0)
    entries: tuple[AgentCatalogEntry, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)
    contract_hash: str = ""

    @model_validator(mode="after")
    def validate_catalog(self) -> Self:
        ids = [entry.definition.agent_id for entry in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError("agent catalog contains duplicate agent_id")
        expected = self.compute_contract_hash()
        if self.contract_hash and self.contract_hash != expected:
            raise ValueError("agent catalog contract hash does not match its content")
        self.contract_hash = expected
        return self

    def compute_contract_hash(self) -> str:
        return _contract_hash(
            {
                "schema_version": self.schema_version,
                "revision": self.revision,
                "entries": [entry.model_dump(mode="json") for entry in self.entries],
            }
        )

    def resolve(self, agent_id: str) -> AgentCatalogEntry:
        for entry in self.entries:
            if entry.definition.agent_id == agent_id and entry.available:
                return entry
        raise KeyError(f"unknown or unavailable agent definition: {agent_id}")


class DelegatedConstraints(OuroModel):
    allowed_tool_names: tuple[str, ...] = ()
    workspace: str | None = None
    allow_write: bool = False
    allow_network: bool = False
    allow_mcp: bool = False
    allow_external_paths: bool = False


class ResultDelivery(OuroModel):
    delivery_id: str = Field(default_factory=lambda: new_id("delivery"))
    policy: DeliveryPolicy = "notify_only"
    status: DeliveryStatus = "pending"
    target_run_id: str | None = None
    target_message_id: str | None = None
    target_queue_id: str | None = None
    delivered_at: datetime | None = None
    error: str | None = None

    @model_validator(mode="after")
    def validate_delivery(self) -> Self:
        if self.status == "delivered":
            if self.delivered_at is None:
                raise ValueError("delivered result requires delivered_at")
            if not any((self.target_run_id, self.target_message_id, self.target_queue_id)):
                raise ValueError("delivered result requires a target identity")
        elif self.delivered_at is not None:
            raise ValueError("only delivered result can have delivered_at")
        return self


class DelegationResult(OuroModel):
    child_session_id: str
    child_run_id: str
    terminal_status: Literal["completed", "failed", "aborted", "paused", "recoverable"]
    result_message_id: str | None = None
    resource_refs: tuple[str, ...] = ()
    usage: dict[str, int | float] = Field(default_factory=dict)
    result_ready_at: datetime = Field(default_factory=utc_now)
    delivery: ResultDelivery = Field(default_factory=ResultDelivery)


class DelegationRecord(OuroModel):
    schema_version: int = SCHEMA_VERSION
    revision: int = Field(default=0, ge=0)
    delegation_id: str = Field(default_factory=lambda: new_id("dlg"))
    idempotency_key: str = Field(min_length=1, max_length=256)
    root_session_id: str
    parent_session_id: str
    parent_run_id: str
    parent_tool_call_id: str | None = None
    child_session_id: str | None = None
    current_child_run_id: str | None = None
    agent_id: str
    nickname: str | None = Field(default=None, max_length=80)
    task_description: str = Field(min_length=1, max_length=500)
    initial_message_id: str | None = None
    prompt_digest: str = Field(min_length=1, max_length=256)
    execution_mode: ExecutionMode = "background"
    lifecycle_binding: LifecycleBinding = "bound"
    delivery_policy: DeliveryPolicy = "notify_only"
    depth: int = Field(default=1, ge=1, le=32)
    spawn_state: SpawnState = "requested"
    control_state: ControlState = "open"
    definition: AgentDefinition
    effective_tools: tuple[EffectiveToolRef, ...] = ()
    constraints: DelegatedConstraints = Field(default_factory=DelegatedConstraints)
    approval_mode: SessionApprovalMode = "inherit"
    interaction_mode: SessionInteractionMode = "interactive"
    budget: RunBudgetPolicy = Field(default_factory=RunBudgetPolicy)
    result: DelegationResult | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    error: str | None = None

    @model_validator(mode="after")
    def validate_relation(self) -> Self:
        if self.agent_id != self.definition.agent_id:
            raise ValueError("delegation agent_id must match frozen definition")
        if self.lifecycle_binding == "detached":
            raise ValueError("detached child is not supported in the first release")
        if self.spawn_state in {"child_created", "submitted"} and self.child_session_id is None:
            raise ValueError("child identity is required after child creation")
        if self.spawn_state == "submitted" and self.current_child_run_id is None:
            raise ValueError("submitted delegation requires current child run identity")
        if self.result is not None:
            if self.child_session_id != self.result.child_session_id:
                raise ValueError("delegation result child session does not match relation")
            if self.current_child_run_id != self.result.child_run_id:
                raise ValueError("delegation result child run does not match relation")
        return self


class AgentHandle(OuroModel):
    delegation_id: str
    child_session_id: str | None = None
    child_run_id: str | None = None
    agent_id: str
    status: str


class AgentWaitResolution(OuroModel):
    resolved_delegation_ids: tuple[str, ...] = ()
    reason: Literal["condition_met", "timeout", "interrupted"]
    resolved_at: datetime = Field(default_factory=utc_now)


class AgentWaitRequest(OuroModel):
    wait_id: str = Field(default_factory=lambda: new_id("wait"))
    delegation_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    completion_condition: WaitCompletionCondition = "all"
    deadline: datetime | None = None
    state: WaitState = "pending"
    resolution: AgentWaitResolution | None = None

    @model_validator(mode="after")
    def validate_wait(self) -> Self:
        if len(set(self.delegation_ids)) != len(self.delegation_ids):
            raise ValueError("delegation ids in a wait request must be unique")
        if (self.state == "resolved") is not (self.resolution is not None):
            raise ValueError("resolved wait state and resolution must agree")
        return self


class AgentControlRequest(OuroModel):
    """A product-owned tool intent interpreted by the application layer."""

    action: AgentControlAction
    request_id: str = Field(min_length=1, max_length=256)
    agent_id: str | None = None
    task: str | None = Field(default=None, max_length=20_000)
    description: str | None = Field(default=None, max_length=500)
    nickname: str | None = Field(default=None, max_length=80)
    execution_mode: ExecutionMode = "background"
    delivery_policy: DeliveryPolicy = "notify_only"
    delegation_id: str | None = None
    delegation_ids: tuple[str, ...] = ()
    completion_condition: WaitCompletionCondition = "all"
    timeout_seconds: float | None = Field(default=None, ge=0, le=86_400)
    message: str | None = Field(default=None, max_length=20_000)
    input_mode: Literal["steer", "follow_up"] = "steer"
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if self.action == "spawn":
            if not self.agent_id or not self.task or not self.task.strip():
                raise ValueError("spawn control requires agent_id and task")
            if not self.description or not self.description.strip():
                raise ValueError("spawn control requires description")
        elif self.action == "wait":
            if not self.delegation_ids:
                raise ValueError("wait control requires delegation_ids")
            if len(set(self.delegation_ids)) != len(self.delegation_ids):
                raise ValueError("wait delegation_ids must be unique")
        elif self.action in {"send", "interrupt"}:
            if not self.delegation_id:
                raise ValueError(f"{self.action} control requires delegation_id")
            if self.action == "send" and (not self.message or not self.message.strip()):
                raise ValueError("send control requires message")
        return self


class AgentControlResult(OuroModel):
    """Application result returned to the ordinary tool-call state machine."""

    status: AgentControlStatus = "completed"
    is_error: bool = False
    content: str = ""
    structured: dict[str, Any] = Field(default_factory=dict)
    wait: AgentWaitRequest | None = None

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if (self.status == "waiting_child") is not (self.wait is not None):
            raise ValueError("waiting_child status and wait request must agree")
        return self


__all__ = [
    "AGENT_CONTROL_TOOL_NAMES",
    "AgentControlRequest",
    "AgentControlResult",
    "AgentCatalogEntry",
    "AgentCatalogSnapshot",
    "AgentDefinition",
    "AgentHandle",
    "AgentWaitRequest",
    "AgentWaitResolution",
    "ContextPolicy",
    "DelegatedConstraints",
    "DelegationRecord",
    "DelegationResult",
    "EffectiveToolRef",
    "ResultDelivery",
    "ToolSelection",
]
