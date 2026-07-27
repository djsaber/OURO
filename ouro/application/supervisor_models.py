from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from ouro.application.dto import ApplicationDTO, Diagnostic
from ouro.application.event_models import RuntimeEventView
from ouro.application.session_models import SessionContextStatus
from ouro.core.base import new_id, utc_now
from ouro.core.state import RunPhase
from ouro.core.tool_batches import ToolBatchPhase, ToolCallStatus

SupervisorRunStatus = Literal[
    "idle",
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
ShutdownMode = Literal["wait", "abort"]
RuntimeActivity = Literal[
    "idle",
    "queued",
    "preparing_model",
    "waiting_model",
    "streaming_reasoning",
    "streaming_text",
    "streaming_tool_call",
    "finalizing_response",
    "preparing_tools",
    "waiting_approval",
    "waiting_user",
    "waiting_child",
    "executing_tools",
    "committing_results",
    "compacting",
    "completed",
    "aborted",
    "failed",
    "paused",
    "recoverable",
]


class RunHandle(ApplicationDTO):
    handle_id: str = Field(default_factory=lambda: new_id("handle"))
    session_id: str
    submitted_at: datetime = Field(default_factory=utc_now)


class HostInputReceipt(ApplicationDTO):
    """Durable acceptance identity for one Host-owned Session input."""

    session_id: str
    queue_id: str
    accepted_at: datetime = Field(default_factory=utc_now)


class RuntimeStatusSnapshot(ApplicationDTO):
    handle: RunHandle | None = None
    session_id: str
    run_id: str | None = None
    status: SupervisorRunStatus
    updated_at: datetime = Field(default_factory=utc_now)
    diagnostics: tuple[Diagnostic, ...] = ()


class RuntimeToolCallActivity(ApplicationDTO):
    tool_call_id: str
    tool_name: str
    status: ToolCallStatus
    source_index: int = Field(ge=0)


class RuntimeActivitySnapshot(ApplicationDTO):
    session_id: str
    run_id: str | None = None
    status: SupervisorRunStatus
    activity: RuntimeActivity
    run_phase: RunPhase | None = None
    turn_id: str | None = None
    batch_id: str | None = None
    batch_phase: ToolBatchPhase | None = None
    tool_calls: tuple[RuntimeToolCallActivity, ...] = ()
    compaction_id: str | None = None
    context_status: SessionContextStatus | None = None
    sequence: int = Field(default=0, ge=0)
    updated_at: datetime = Field(default_factory=utc_now)


class ShutdownPolicy(ApplicationDTO):
    mode: ShutdownMode = "wait"
    timeout_seconds: float = Field(default=10.0, gt=0)


class RuntimeNotification(ApplicationDTO):
    kind: Literal["event", "activity", "diagnostic"]
    event: RuntimeEventView | None = None
    activity: RuntimeActivitySnapshot | None = None
    diagnostic: Diagnostic | None = None

    @model_validator(mode="after")
    def _validate_payload(self) -> RuntimeNotification:
        payloads = {
            "event": self.event,
            "activity": self.activity,
            "diagnostic": self.diagnostic,
        }
        if payloads[self.kind] is None:
            raise ValueError(f"runtime notification {self.kind!r} is missing its payload")
        if sum(value is not None for value in payloads.values()) != 1:
            raise ValueError("runtime notification must contain exactly one payload")
        return self


class SupervisorStartResult(ApplicationDTO):
    recovered: tuple[RuntimeStatusSnapshot, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
