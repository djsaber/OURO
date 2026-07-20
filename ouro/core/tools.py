from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import Field, JsonValue, model_validator

from ouro.core.approvals import ApprovalRecord, PermissionMode, ToolPermissionControl
from ouro.core.base import Metadata, MetadataField, OuroModel, new_id, utc_now

ToolExecutionPreference = Literal["inherit", "sequential"]
ToolEffect = Literal["read_only", "state_changing", "unknown"]
ToolBatchBehavior = Literal["compatible", "exclusive"]
ToolKind = Literal["standard", "user_input"]
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
    permission_control: ToolPermissionControl = "host"
    result_schema: dict[str, Any] = Field(default_factory=dict)
    can_abort: bool = True
    execution_mode: ToolExecutionPreference = "inherit"
    effect: ToolEffect = "unknown"
    batch_behavior: ToolBatchBehavior = "compatible"
    resumable: bool = False
    supports_unattended: bool = False
    kind: ToolKind = "standard"
    metadata: Metadata = MetadataField

    @model_validator(mode="after")
    def validate_permission_control(self) -> Self:
        _validate_permission_control(self.permission_mode, self.permission_control)
        if self.supports_unattended and not self.resumable:
            raise ValueError("unattended tool input handling requires a resumable tool")
        return self


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
    canonical_tool_id: str | None = None
    owner_id: str | None = None
    source: ToolSource = "builtin"
    effect: ToolEffect = "unknown"
    batch_behavior: ToolBatchBehavior = "compatible"
    arguments: dict[str, Any] = Field(default_factory=dict)
    requested_at: datetime = Field(default_factory=utc_now)
    permission_mode: PermissionMode = "auto"
    permission_control: ToolPermissionControl = "host"
    approval_id: str | None = None
    plugin_id: str | None = None
    plugin_version: str | None = None
    plugin_class: Literal["builtin", "user"] | None = None
    release_id: str | None = None
    package_digest: str | None = None
    builtin_contract_hash: str | None = None
    generation_id: str | None = None
    contract_hash: str | None = None
    execution_contract_hash: str | None = None
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


class ToolInputRequest(OuroModel):
    """Opaque JSON data that a tool asks its host to present to the user."""

    data: JsonValue


class ToolExecutionOutcome(OuroModel):
    status: ToolOutcomeStatus
    invocation: ToolInvocation
    result: ToolResult | None = None
    approval: ApprovalRecord | None = None
    input_request: ToolInputRequest | None = None

    @property
    def question(self) -> str | None:
        """Return the legacy text question when the request happens to be text."""

        if self.input_request is None or not isinstance(self.input_request.data, str):
            return None
        return self.input_request.data


def _validate_permission_control(
    permission_mode: PermissionMode,
    permission_control: ToolPermissionControl,
) -> None:
    if permission_control == "tool" and permission_mode == "approval":
        raise ValueError("tool-controlled permission must be auto or deny")
