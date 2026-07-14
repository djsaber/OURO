from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from ouro.core.base import SCHEMA_VERSION, Metadata, MetadataField, OuroModel, new_id, utc_now

EventType = Literal[
    "session_loaded",
    "run_started",
    "run_status_changed",
    "user_message_added",
    "model_request_started",
    "model_text_delta",
    "model_reasoning_delta",
    "model_tool_call_delta",
    "model_message_completed",
    "tool_batch_planned",
    "tool_batch_started",
    "tool_batch_completed",
    "tool_batch_aborted",
    "tool_call_requested",
    "approval_requested",
    "approval_resolved",
    "tool_call_started",
    "tool_call_completed",
    "ask_user_requested",
    "ask_user_resolved",
    "run_retry_scheduled",
    "compaction_started",
    "compaction_progress",
    "compaction_completed",
    "compaction_failed",
    "compaction_skipped",
    "run_completed",
    "run_aborted",
    "run_paused",
    "run_failed",
    "recovery_tool_result_committed",
    "persistence_warning",
    "plugin_hook_failed",
    "diagnostic",
]


class EventRecord(OuroModel):
    schema_version: int = SCHEMA_VERSION
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    session_id: str
    run_id: str | None = None
    turn_id: str | None = None
    event_type: EventType | str
    created_at: datetime = Field(default_factory=utc_now)
    sequence: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: Metadata = MetadataField
