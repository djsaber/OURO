from __future__ import annotations

from typing import Literal

from pydantic import Field

from ouro.application.dto import ApplicationDTO
from ouro.application.supervisor_models import RuntimeActivitySnapshot
from ouro.core.agents import (
    DeliveryPolicy,
    ExecutionMode,
    LifecycleBinding,
    ToolSelection,
)
from ouro.core.state import RunStatus


class AgentDefinitionView(ApplicationDTO):
    agent_id: str
    name: str
    description: str
    tool_selection: ToolSelection
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
    effective_from: Literal["next_run"] = "next_run"


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
    execution_mode: ExecutionMode = "background"
    lifecycle_binding: LifecycleBinding = "bound"
    delivery_policy: DeliveryPolicy = "notify_only"


class AgentHandleView(ApplicationDTO):
    delegation_id: str
    child_session_id: str | None = None
    child_run_id: str | None = None
    agent_id: str
    agent_name: str
    nickname: str | None = None
    status: str
    execution_mode: ExecutionMode


class AgentTreeNode(ApplicationDTO):
    delegation_id: str
    session_id: str
    parent_session_id: str
    agent_id: str
    agent_name: str
    nickname: str | None = None
    task_description: str
    status: str
    execution_mode: ExecutionMode
    depth: int
    activity: RuntimeActivitySnapshot | None = None


class AgentTreeSnapshot(ApplicationDTO):
    root_session_id: str
    revision: int = Field(ge=0)
    nodes: tuple[AgentTreeNode, ...] = ()
    active_count: int = Field(default=0, ge=0)
    waiting_count: int = Field(default=0, ge=0)
    terminal_count: int = Field(default=0, ge=0)


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


class AgentReadView(ApplicationDTO):
    handle: AgentHandleView
    final_text: str = ""
    result_message_id: str | None = None
    terminal_status: RunStatus | None = None


__all__ = [
    "AgentDefinitionCatalogView",
    "AgentDefinitionDetail",
    "AgentDefinitionView",
    "AgentToolOptionView",
    "AgentHandleView",
    "AgentInteractionView",
    "AgentReadView",
    "AgentTreeNode",
    "AgentTreeSnapshot",
    "AgentWaitResultView",
    "SpawnAgentRequest",
]
