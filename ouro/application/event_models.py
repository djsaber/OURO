from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from ouro.application.dto import ApplicationDTO
from ouro.core.events import EventRecord
from ouro.core.redaction import redact_data

RuntimeEventCategory = Literal[
    "text",
    "reasoning",
    "tool",
    "interaction",
    "run",
    "compaction",
    "lifecycle",
    "unknown",
]


class RuntimeEventView(ApplicationDTO):
    event_id: str
    session_id: str
    run_id: str | None = None
    turn_id: str | None = None
    event_type: str
    category: RuntimeEventCategory
    created_at: datetime
    sequence: int
    text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    status: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


def project_runtime_event(event: EventRecord) -> RuntimeEventView:
    category: RuntimeEventCategory = _event_category(event.event_type)
    payload = event.payload
    text: str | None = None
    if event.event_type == "model_text_delta":
        text = _string(payload.get("text"))
    elif event.event_type == "model_reasoning_delta":
        text = _string(payload.get("reasoning_content"))
    status = _string(
        payload.get("status") or payload.get("to_status") or payload.get("tool_call_status")
    )
    if status is None:
        status = {
            "run_completed": "completed",
            "run_aborted": "aborted",
            "run_failed": "failed",
            "run_paused": "paused",
        }.get(event.event_type)
    redacted = redact_data(payload)
    assert isinstance(redacted, dict)
    return RuntimeEventView(
        event_id=event.event_id,
        session_id=event.session_id,
        run_id=event.run_id,
        turn_id=event.turn_id,
        event_type=event.event_type,
        category=category,
        created_at=event.created_at,
        sequence=event.sequence,
        text=text,
        tool_call_id=_string(payload.get("tool_call_id")),
        tool_name=_string(payload.get("tool_name")),
        status=status,
        data=redacted,
    )


def validate_runtime_event_payload(event_type: str, payload: dict[str, Any]) -> None:
    required = {
        "model_text_delta": ("text",),
        "model_reasoning_delta": ("reasoning_content",),
        "model_message_completed": ("finish_reason",),
        "tool_call_requested": ("tool_call_id",),
        "tool_policy_evaluated": ("tool_call_id",),
        "tool_call_started": ("tool_call_id",),
        "tool_call_completed": ("tool_call_id",),
        "run_status_changed": ("to_status",),
        "follow_up_input_queued": ("queue_id",),
        "steering_input_queued": ("queue_id",),
        "queued_input_cancelled": ("queue_id",),
        "queued_input_consumed": ("queue_id",),
        "turn_policy_continued": ("turn_id",),
        "run_continuation_resolved": ("decision_id",),
        "agent_spawned": (
            "delegation_id",
            "child_session_id",
            "child_run_id",
            "agent_id",
            "execution_mode",
        ),
        "agent_result_ready": (
            "delegation_id",
            "child_session_id",
            "child_run_id",
            "terminal_status",
            "delivery_id",
        ),
        "agent_result_delivered": ("delegation_id", "delivery_id", "policy"),
        "agent_closed": ("delegation_id",),
        "compaction_model_output": (
            "compaction_id",
            "request_id",
            "route",
            "status",
            "finish_reason",
            "raw_text",
            "raw_reasoning",
        ),
    }
    for field in required.get(event_type, ()):
        if not isinstance(payload.get(field), str):
            raise ValueError(f"runtime event {event_type} requires string payload field {field}")
    if event_type == "compaction_model_output":
        for field in (
            "chunk_index",
            "attempt_index",
            "requested_output_tokens",
            "estimated_input_tokens",
            "input_message_count",
            "input_frame_count",
        ):
            if not isinstance(payload.get(field), int) or isinstance(payload.get(field), bool):
                raise ValueError(
                    f"runtime event {event_type} requires integer payload field {field}"
                )
        for field in ("summary_opened", "summary_closed"):
            if not isinstance(payload.get(field), bool):
                raise ValueError(
                    f"runtime event {event_type} requires boolean payload field {field}"
                )


def _event_category(event_type: str) -> RuntimeEventCategory:
    if event_type == "model_text_delta":
        return "text"
    if event_type == "model_reasoning_delta":
        return "reasoning"
    if event_type.startswith("tool_") or event_type == "model_tool_call_delta":
        return "tool"
    if event_type.startswith(("approval_", "ask_user_", "queued_input_")) or event_type in {
        "follow_up_input_queued",
        "steering_input_queued",
    }:
        return "interaction"
    if event_type.startswith("run_"):
        return "run"
    if event_type.startswith("compaction_"):
        return "compaction"
    if event_type in {
        "session_loaded",
        "user_message_added",
        "host_message_added",
        "plugin_message_added",
        "model_request_started",
        "model_message_completed",
        "recovery_tool_result_committed",
        "persistence_warning",
        "plugin_observer_failed",
        "context_provider_failed",
        "turn_policy_continued",
        "turn_policy_failed",
        "diagnostic",
        "event_sink_failed",
        "event_observer_overflow",
    }:
        return "lifecycle"
    return "unknown"


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None
