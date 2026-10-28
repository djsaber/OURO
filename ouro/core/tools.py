from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import ConfigDict, Field, JsonValue, model_validator

from ouro.core.agents import AgentControlRequest, AgentWaitRequest
from ouro.core.approvals import ApprovalRecord, PermissionMode, ToolPermissionControl
from ouro.core.base import Metadata, MetadataField, OuroModel, new_id, utc_now
from ouro.core.content import (
    ContentPart,
    ToolContent,
    append_content_text,
    content_parts,
    content_text,
    media_resource_ids,
    same_content_observation,
)
from ouro.core.harness_calls import HarnessStateChange
from ouro.core.session_controls import (
    SessionControlTransitionRequest,
    SessionControlTransitionResult,
)

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
ToolOutcomeStatus = Literal[
    "ready",
    "control_requested",
    "completed",
    "approval_required",
    "waiting_user",
    "waiting_child",
    "aborted",
    "unknown_outcome",
]


class ToolDefinition(OuroModel):
    name: str
    display_name: str | None = None
    description: str
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    source: ToolSource = "builtin"
    permission_mode: PermissionMode = "auto"
    permission_control: ToolPermissionControl = "host"
    result_schema: dict[str, Any] = Field(default_factory=dict)
    execution_mode: ToolExecutionPreference = "inherit"
    effect: ToolEffect = "unknown"
    batch_behavior: ToolBatchBehavior = "compatible"
    resumable: bool = False
    supports_unattended: bool = False
    active_when_controller_id: str | None = None
    interaction_controller_id: str | None = None
    kind: ToolKind = "standard"
    metadata: Metadata = MetadataField

    @model_validator(mode="after")
    def validate_tool_contract(self) -> Self:
        _validate_permission_control(self.permission_mode, self.permission_control)
        if self.supports_unattended and not self.resumable:
            raise ValueError("unattended tool input handling requires a resumable tool")
        if self.interaction_controller_id is not None and not self.resumable:
            raise ValueError("interaction controller binding requires a resumable tool")
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


class ToolCallProducer(OuroModel):
    kind: Literal["model", "driver"] = "model"
    driver_id: str | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if (self.kind == "driver") != bool(self.driver_id):
            raise ValueError("only Driver tool calls require a Driver identity")
        return self


class ToolInvocationBinding(OuroModel):
    """Execution provenance, separate from the arguments owned by a Call."""

    model_config = ConfigDict(extra="forbid")

    producer: ToolCallProducer = Field(default_factory=ToolCallProducer)
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
    requested_at: datetime = Field(default_factory=utc_now)
    permission_mode: PermissionMode = "auto"
    permission_control: ToolPermissionControl = "host"
    resumable: bool | None = None
    definition_contract_hash: str | None = None
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
    active_when_controller_id: str | None = None
    interaction_controller_id: str | None = None
    interaction_activation_id: str | None = None
    tool_policy_ids: list[str] = Field(default_factory=list)
    tool_policy_reason: str | None = None


class ToolInvocation(ToolInvocationBinding):
    model_config = ConfigDict(extra="ignore")

    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResultValue(OuroModel):
    """An observed value; accepting it does not apply any business proposal."""

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str
    tool_name: str
    is_error: bool = False
    content: ToolContent = ""
    structured: dict[str, Any] = Field(default_factory=dict)
    resource_refs: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    exit_code: int | None = None
    metadata: Metadata = MetadataField

    @property
    def content_blocks(self) -> list[ContentPart]:
        return content_parts(self.content)

    @property
    def text_content(self) -> str:
        return content_text(self.content)

    def same_observation(self, other: ToolResultValue) -> bool:
        return self.model_copy(update={"content": ""}) == other.model_copy(
            update={"content": ""}
        ) and same_content_observation(self.content, other.content)

    @property
    def referenced_resource_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([*self.resource_refs, *media_resource_ids(self.content_blocks)]))

    @model_validator(mode="before")
    @classmethod
    def default_unknown_duration_to_instant(cls, value: Any) -> Any:
        """Keep terminal-only results from inventing an execution interval."""

        if (
            isinstance(value, Mapping)
            and "started_at" not in value
            and value.get("ended_at") is not None
        ):
            return {**value, "started_at": value["ended_at"]}
        return value


class ToolResultFeedback(OuroModel):
    """Framework settlement feedback, separately owned from the tool's observation."""

    model_config = ConfigDict(extra="forbid")

    controls: tuple[SessionControlTransitionResult, ...] = ()
    error_type: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def validate_feedback(self) -> Self:
        if (self.error_type is None) != (self.error_message is None):
            raise ValueError("settlement feedback requires both error type and message")
        if not self.controls and self.error_type is None:
            raise ValueError("settlement feedback cannot be empty")
        return self

    def materialize[ResultT: ToolResultValue](self, result: ResultT) -> ResultT:
        output = [item.output for item in self.controls if item.output]
        structured = dict(result.structured)
        if self.controls:
            structured["session_controls"] = [
                item.model_dump(mode="json") for item in self.controls
            ]
        if self.error_type is not None:
            assert self.error_message is not None
            output.append(self.error_message)
            structured.update(error_type=self.error_type, error_message=self.error_message)
        return result.model_copy(
            update={
                "content": append_content_text(result.content, "\n".join(output)),
                "structured": structured,
                "is_error": result.is_error or self.error_type is not None,
            }
        )


class ToolResult(ToolResultValue):
    model_config = ConfigDict(extra="ignore")

    harness_change: HarnessStateChange | None = None
    session_control_transitions: list[SessionControlTransitionRequest] = Field(default_factory=list)


class ToolInputRequest(OuroModel):
    """Opaque JSON data that a tool asks its host to present to the user."""

    harness_change: HarnessStateChange | None = None
    data: JsonValue


class ToolExecutionOutcome(OuroModel):
    status: ToolOutcomeStatus
    invocation: ToolInvocation
    result: ToolResult | None = None
    approval: ApprovalRecord | None = None
    input_request: ToolInputRequest | None = None
    agent_wait: AgentWaitRequest | None = None
    control_request: AgentControlRequest | None = None
    feedback: ToolResultFeedback | None = None

    @model_validator(mode="after")
    def validate_control_request(self) -> Self:
        if (self.status == "control_requested") != (self.control_request is not None):
            raise ValueError("control_requested requires an explicit control request")
        if self.control_request is not None and self.result is not None:
            raise ValueError("a control request is not an observed tool result")
        return self


def tool_definition_contract_hash(definition: ToolDefinition) -> str:
    encoded = json.dumps(
        definition.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_permission_control(
    permission_mode: PermissionMode,
    permission_control: ToolPermissionControl,
) -> None:
    if permission_control == "tool" and permission_mode == "approval":
        raise ValueError("tool-controlled permission must be auto or deny")
