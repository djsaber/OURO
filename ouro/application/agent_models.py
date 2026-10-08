from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from ouro.application.dto import ApplicationDTO, Diagnostic
from ouro.application.supervisor_models import RuntimeActivitySnapshot
from ouro.core.agents import (
    ControlState,
    DeliveryStatus,
    ExecutionMode,
    LifecycleBinding,
    ToolSelection,
)
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.state import RunStatus


class AgentDefinitionView(ApplicationDTO):
    agent_id: str
    name: str
    description: str
    tool_selection: ToolSelection
    model: str | None = None
    source: Literal["builtin", "user"]
    editable: bool
    compatibility: Literal["available", "unavailable"]
    missing_tool_names: tuple[str, ...] = ()
    contract_hash: str


class AgentDefinitionDetail(AgentDefinitionView):
    system_prompt: str


class AgentDefinitionCatalogView(ApplicationDTO):
    revision: int
    definitions: tuple[AgentDefinitionView, ...] = ()
    effective_from: Literal["next_decision"] = "next_decision"


class AgentToolOptionView(ApplicationDTO):
    name: str
    description: str
    effect: Literal["read_only", "state_changing", "unknown"]
    owner_id: str
    execution_contract_hash: str


class SpawnAgentRequest(ApplicationDTO):
    request_id: str = Field(min_length=1, max_length=256)
    parent_session_id: str
    parent_run_id: str
    agent_id: str
    task: str = Field(min_length=1, max_length=20_000)
    description: str = Field(min_length=1, max_length=500)
    nickname: str | None = Field(default=None, max_length=80)
    parent_tool_call_id: str | None = None
    execution_mode: ExecutionMode = "sync"
    lifecycle_binding: LifecycleBinding = "bound"
    unattended: bool = False
    resource_refs: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("resource_refs")
    @classmethod
    def validate_reference_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("Agent resource references cannot be blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Agent resource references must be unique")
        return normalized


class AgentHandleView(ApplicationDTO):
    revision: int = Field(ge=0)
    delegation_id: str
    root_session_id: str
    parent_session_id: str
    parent_run_id: str
    parent_tool_call_id: str | None = None
    child_session_id: str | None = None
    child_run_id: str | None = None
    agent_id: str
    agent_name: str
    nickname: str | None = None
    status: str
    execution_mode: ExecutionMode
    control_state: ControlState


class AgentInteractionSummary(ApplicationDTO):
    kind: Literal["approval", "user_input"]
    tool_call_id: str
    tool_name: str
    request_id: str
    requested_at: datetime


class AgentTreeEdge(ApplicationDTO):
    delegation_id: str
    parent_session_id: str
    child_session_id: str


class AgentTreeNode(ApplicationDTO):
    revision: int = Field(ge=0)
    delegation_id: str
    root_session_id: str
    session_id: str
    parent_session_id: str
    parent_run_id: str
    parent_tool_call_id: str | None = None
    agent_id: str
    agent_name: str
    nickname: str | None = None
    task_description: str
    status: str
    execution_mode: ExecutionMode
    lifecycle_binding: LifecycleBinding
    control_state: ControlState
    depth: int
    usage: dict[str, int | float] = Field(default_factory=dict)
    budget: RunBudgetPolicy = Field(default_factory=RunBudgetPolicy)
    pending_interactions: tuple[AgentInteractionSummary, ...] = ()
    result_ready: bool = False
    delivery_status: DeliveryStatus | None = None
    activity: RuntimeActivitySnapshot | None = None


class AgentTreeSnapshot(ApplicationDTO):
    root_session_id: str
    revision: int = Field(ge=0)
    nodes: tuple[AgentTreeNode, ...] = ()
    edges: tuple[AgentTreeEdge, ...] = ()
    root_usage: dict[str, int | float] = Field(default_factory=dict)
    root_budget: RunBudgetPolicy | None = None
    root_activity: RuntimeActivitySnapshot | None = None
    waiting_on_delegation_ids: tuple[str, ...] = ()
    active_count: int = Field(default=0, ge=0)
    waiting_count: int = Field(default=0, ge=0)
    terminal_count: int = Field(default=0, ge=0)
    pending_interaction_count: int = Field(default=0, ge=0)


class AgentTreeNotification(ApplicationDTO):
    kind: Literal["snapshot", "diagnostic"]
    snapshot: AgentTreeSnapshot | None = None
    diagnostic: Diagnostic | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if self.kind == "snapshot" and self.snapshot is None:
            raise ValueError("Agent tree snapshot notification requires a snapshot")
        if self.kind == "diagnostic" and self.diagnostic is None:
            raise ValueError("Agent tree diagnostic notification requires a diagnostic")
        if (self.snapshot is None) == (self.diagnostic is None):
            raise ValueError("Agent tree notification requires exactly one payload")
        return self


class AgentWaitResultView(ApplicationDTO):
    handles: tuple[AgentHandleView, ...]
    completed: bool


class AgentInteractionView(ApplicationDTO):
    kind: Literal["approval", "user_input"]
    delegation_id: str
    agent_id: str
    agent_name: str
    task_description: str
    child_session_id: str
    child_run_id: str
    tool_call_id: str
    tool_name: str
    request_id: str
    requested_at: str
    arguments_preview: str = ""
    input_request: JsonValue = None
    canonical_tool_id: str | None = None
    plugin_id: str | None = None
    plugin_class: Literal["builtin", "user"] | None = None


class AgentReadView(ApplicationDTO):
    handle: AgentHandleView
    final_text: str = ""
    result_message_id: str | None = None
    terminal_status: RunStatus | None = None
    error: str | None = None


__all__ = [
    "AgentDefinitionCatalogView",
    "AgentDefinitionDetail",
    "AgentDefinitionView",
    "AgentToolOptionView",
    "AgentHandleView",
    "AgentInteractionSummary",
    "AgentInteractionView",
    "AgentReadView",
    "AgentTreeEdge",
    "AgentTreeNode",
    "AgentTreeNotification",
    "AgentTreeSnapshot",
    "AgentWaitResultView",
    "SpawnAgentRequest",
]
