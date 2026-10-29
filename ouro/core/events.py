from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from ouro.core.base import Metadata, MetadataField, VersionedOuroModel, new_id, utc_now
from ouro.core.response_drafts import ResponseStreamPosition
from ouro.core.session_observation import SessionObservationPosition

EventType = Literal[
    "session_loaded",
    "run_started",
    "run_abort_requested",
    "run_status_changed",
    "user_message_added",
    "host_message_added",
    "plugin_message_added",
    "model_request_started",
    "model_text_delta",
    "model_reasoning_delta",
    "model_tool_call_delta",
    "model_message_completed",
    "model_response_committed",
    "tool_batch_planned",
    "tool_batch_rejected",
    "tool_batch_started",
    "tool_batch_results_committing",
    "tool_batch_completed",
    "tool_batch_aborted",
    "tool_call_requested",
    "tool_policy_evaluated",
    "approval_requested",
    "approval_resolved",
    "tool_call_started",
    "tool_call_completed",
    "ask_user_requested",
    "ask_user_resolved",
    "follow_up_input_queued",
    "steering_input_queued",
    "queued_input_consumed",
    "turn_policy_continued",
    "turn_policy_failed",
    "run_continuation_resolved",
    "agent_spawned",
    "agent_wait_requested",
    "agent_wait_resolved",
    "agent_result_ready",
    "agent_result_delivered",
    "agent_closed",
    "run_retry_scheduled",
    "compaction_started",
    "compaction_progress",
    "compaction_model_output",
    "compaction_completed",
    "compaction_failed",
    "compaction_skipped",
    "run_completed",
    "run_aborted",
    "run_paused",
    "run_failed",
    "recovery_tool_result_committed",
    "persistence_warning",
    "plugin_observer_failed",
    "event_sink_failed",
    "event_observer_overflow",
    "diagnostic",
]

# Live model observations; only current text/reasoning prefixes are recoverable.
MODEL_STREAM_DELTA_EVENT_TYPES = frozenset(
    {
        "model_text_delta",
        "model_reasoning_delta",
        "model_tool_call_delta",
    }
)


class EventRecord(VersionedOuroModel):
    schema_version: int = 2
    history_epoch: int = Field(default=0, ge=0)
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    session_id: str
    run_id: str | None = None
    turn_id: str | None = None
    event_type: EventType | str
    created_at: datetime = Field(default_factory=utc_now)
    sequence: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: Metadata = MetadataField
    stream: ResponseStreamPosition | None = None
    position: SessionObservationPosition | None = None
    commit_revision: int | None = None
    message_after: int | None = None
