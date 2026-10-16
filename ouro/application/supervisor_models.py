from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from ouro.application.dto import ApplicationDTO, Diagnostic
from ouro.application.event_models import RuntimeEventView
from ouro.application.session_models import SessionContextStatus
from ouro.core.base import new_id, utc_now
from ouro.core.context import ContextMetrics
from ouro.core.state import RunPhase
from ouro.core.tool_batches import ToolBatchPhase, ToolCallStatus

RuntimeToolCallStatus = ToolCallStatus | Literal["streaming", "cancelling"]

# Runtime snapshots retain a bounded, verbatim suffix rather than an ever-growing
# incomplete JSON document. Hosts can consume or ignore it without taking on work
# proportional to the total streamed argument size.
RUNTIME_TOOL_ARGUMENT_TAIL_MAX_CHARS = 256

SupervisorRunStatus = Literal[
    "idle",
    "queued",
    "running",
    "cancelling",
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
    "cancelling",
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
    message_id: str | None = None
    run_id: str | None = None


class RuntimeStatusSnapshot(ApplicationDTO):
    handle: RunHandle | None = None
    session_id: str
    run_id: str | None = None
    status: SupervisorRunStatus
    updated_at: datetime = Field(default_factory=utc_now)
    diagnostics: tuple[Diagnostic, ...] = ()


class RuntimeToolCallActivity(ApplicationDTO):
    """Host-neutral projection of a tool call's current runtime activity.

    While ``status`` is ``streaming``, ``tool_call_id`` is a stream-scoped
    projection identity and ``tool_name`` contains the received name fragments.
    ``status`` owns the lifecycle; ``argument_stream_tail`` is bounded preview
    content retained only while the call is ``streaming``. Canonical calls use
    ``arguments`` instead.
    """

    tool_call_id: str
    tool_name: str
    status: RuntimeToolCallStatus
    source_index: int = Field(ge=0)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    argument_stream_tail: str | None = Field(
        default=None,
        max_length=RUNTIME_TOOL_ARGUMENT_TAIL_MAX_CHARS,
        description=(
            "Bounded verbatim suffix of streaming arguments; "
            "None before argument streaming and outside the streaming state"
        ),
    )
    result_data: dict[str, JsonValue] = Field(default_factory=dict)
    requested_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


class RuntimeActivitySnapshot(ApplicationDTO):
    history_epoch: int = 0
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
    model_attempt: int | None = Field(default=None, ge=2)
    model_max_attempts: int | None = Field(default=None, ge=2)
    context_status: SessionContextStatus | None = None
    effective_context_metrics: ContextMetrics | None = None
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
