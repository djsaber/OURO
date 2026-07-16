from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from ouro.core.approvals import ApprovalRecord
from ouro.core.base import Metadata, MetadataField, OuroModel, new_id, utc_now

PermissionMode = Literal["auto", "approval", "deny"]
ToolExecutionPreference = Literal["inherit", "sequential"]
ToolSource = Literal[
    "builtin",
    "official_plugin",
    "third_party_plugin",
    "ai_generated_plugin",
    "mcp",
]
ToolOutcomeStatus = Literal["ready", "completed", "approval_required", "waiting_user", "aborted"]


class ToolDefinition(OuroModel):
    name: str
    display_name: str | None = None
    description: str
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    source: ToolSource = "builtin"
    permission_mode: PermissionMode = "auto"
    result_schema: dict[str, Any] = Field(default_factory=dict)
    can_abort: bool = True
    execution_mode: ToolExecutionPreference = "inherit"
    metadata: Metadata = MetadataField


class ToolCallRequest(OuroModel):
    id: str = Field(default_factory=lambda: new_id("tool"))
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    source_index: int = Field(default=0, ge=0)

    @property
    def tool_call_id(self) -> str:
        return self.id

    @property
    def tool_name(self) -> str:
        return self.name


class ToolInvocation(OuroModel):
    tool_call_id: str = Field(default_factory=lambda: new_id("tool"))
    session_id: str
    run_id: str
    turn_id: str | None = None
    tool_name: str
    source: ToolSource = "builtin"
    arguments: dict[str, Any] = Field(default_factory=dict)
    requested_at: datetime = Field(default_factory=utc_now)
    permission_mode: PermissionMode = "auto"
    approval_id: str | None = None
    plugin_id: str | None = None
    plugin_version: str | None = None
    package_digest: str | None = None
    generation_id: str | None = None
    contract_hash: str | None = None
    tool_policy_ids: list[str] = Field(default_factory=list)
    tool_policy_reason: str | None = None


class ToolResult(OuroModel):
    tool_call_id: str
    tool_name: str
    is_error: bool = False
    content: str = ""
    structured: dict[str, Any] = Field(default_factory=dict)
    resource_refs: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    exit_code: int | None = None
    metadata: Metadata = MetadataField


class ToolExecutionOutcome(OuroModel):
    status: ToolOutcomeStatus
    invocation: ToolInvocation
    result: ToolResult | None = None
    approval: ApprovalRecord | None = None
    question: str | None = None
