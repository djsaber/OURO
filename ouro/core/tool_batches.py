from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from ouro.core.approvals import ApprovalRecord
from ouro.core.base import Metadata, MetadataField, OuroModel, new_id, utc_now
from ouro.core.messages import MessageRecord
from ouro.core.tools import ToolCallRequest, ToolInvocation, ToolResult

ToolExecutionMode = Literal["sequential", "parallel"]
ToolBatchPhase = Literal[
    "planned",
    "awaiting_interaction",
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
    "ready",
    "rejected",
    "running",
    "completed",
    "failed",
    "aborted",
    "unknown_outcome",
]
ToolBatchOutcomeStatus = Literal["completed", "waiting_approval", "waiting_user"]


class ToolBatchCall(OuroModel):
    request: ToolCallRequest
    status: ToolCallStatus = "planned"
    invocation: ToolInvocation | None = None
    approval: ApprovalRecord | None = None
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
        return all(call.status not in {"awaiting_approval", "waiting_user"} for call in self.calls)


class ToolBatchHandlingResult(OuroModel):
    status: ToolBatchOutcomeStatus
    batch: ToolCallBatchRecord
    messages: list[MessageRecord] = Field(default_factory=list)
    results: list[ToolResult] = Field(default_factory=list)
    question: str | None = None
    sequence: int = 0
