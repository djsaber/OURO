from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from ouro.core.agents import AgentWaitRequest
from ouro.core.approvals import ApprovalRecord
from ouro.core.base import Metadata, MetadataField, OuroModel, new_id, utc_now
from ouro.core.interactions import ToolInputResolution
from ouro.core.messages import MessageRecord
from ouro.core.tools import ToolCallRequest, ToolInputRequest, ToolInvocation, ToolResult

ToolExecutionMode = Literal["sequential", "parallel"]
ToolBatchPhase = Literal[
    "planned",
    "awaiting_interaction",
    "awaiting_child",
    "ready",
    "executing",
    "committing_results",
    "completed",
    "aborted",
    "recoverable",
]
ToolCallStatus = Literal[
    "planned",
    "awaiting_approval",
    "waiting_user",
    "waiting_child",
    "resolving_input",
    "ready",
    "rejected",
    "running",
    "completed",
    "failed",
    "aborted",
    "unknown_outcome",
]
ToolBatchOutcomeStatus = Literal["completed", "waiting_approval", "waiting_user", "waiting_child"]


class ToolBatchCall(OuroModel):
    request: ToolCallRequest
    status: ToolCallStatus = "planned"
    invocation: ToolInvocation | None = None
    approval: ApprovalRecord | None = None
    input_request: ToolInputRequest | None = None
    input_resolution: ToolInputResolution | None = None
    input_revision: int = Field(default=0, ge=0)
    agent_wait: AgentWaitRequest | None = None
    result: ToolResult | None = None
    result_message_id: str | None = None
    metadata: Metadata = MetadataField


class ToolCallBatchRecord(OuroModel):
    batch_id: str = Field(default_factory=lambda: new_id("batch"))
    turn_id: str
    assistant_message_id: str
    phase: ToolBatchPhase = "planned"
    execution_mode: ToolExecutionMode = "sequential"
    max_concurrency: int = Field(default=4, ge=1)
    calls: list[ToolBatchCall] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: Metadata = MetadataField

    @property
    def pending_approvals(self) -> list[ApprovalRecord]:
        return [
            call.approval
            for call in self.calls
            if call.status == "awaiting_approval" and call.approval is not None
        ]

    @property
    def is_interaction_resolved(self) -> bool:
        return all(
            call.status not in {"awaiting_approval", "waiting_user", "waiting_child"}
            for call in self.calls
        )


class ToolBatchHandlingResult(OuroModel):
    status: ToolBatchOutcomeStatus
    batch: ToolCallBatchRecord
    messages: list[MessageRecord] = Field(default_factory=list)
    results: list[ToolResult] = Field(default_factory=list)
    input_request: ToolInputRequest | None = None
    sequence: int = 0
