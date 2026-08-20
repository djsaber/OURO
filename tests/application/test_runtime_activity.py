from ouro.application.runtime_activity import (
    project_runtime_activity,
    queued_runtime_activity,
    reduce_runtime_activity,
    runtime_activity_changed,
)
from ouro.core.context import ContextMetrics
from ouro.core.events import EventRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_batches import ToolBatchCall, ToolCallBatchRecord
from ouro.core.tools import ToolCallRequest
from ouro.sessions.base import SessionSnapshot


def _event(
    sequence: int,
    event_type: str,
    payload: dict[str, object] | None = None,
) -> EventRecord:
    return EventRecord(
        session_id="ses_1",
        run_id="run_1",
        event_type=event_type,
        sequence=sequence,
        payload=payload or {},
    )


def test_runtime_activity_reduces_model_interaction_and_tool_execution() -> None:
    activity = queued_runtime_activity("ses_1")
    transitions = [
        _event(1, "run_started"),
        _event(2, "model_request_started", {"turn_id": "turn_1"}),
        _event(3, "model_reasoning_delta", {"turn_id": "turn_1"}),
        _event(4, "model_text_delta", {"turn_id": "turn_1"}),
        _event(5, "model_tool_call_delta", {"turn_id": "turn_1", "index": 0}),
        _event(
            6,
            "model_message_completed",
            {"turn_id": "turn_1", "tool_call_count": 1},
        ),
        _event(
            7,
            "tool_batch_planned",
            {"turn_id": "turn_1", "batch_id": "batch_1"},
        ),
        _event(
            8,
            "tool_call_requested",
            {
                "turn_id": "turn_1",
                "batch_id": "batch_1",
                "tool_call_id": "call_1",
                "tool_name": "Read",
                "source_index": 0,
                "arguments": {"path": "README.md"},
            },
        ),
        _event(
            9,
            "approval_requested",
            {"turn_id": "turn_1", "batch_id": "batch_1", "tool_call_id": "call_1"},
        ),
        _event(
            10,
            "run_status_changed",
            {"turn_id": "turn_1", "batch_id": "batch_1", "to_status": "waiting_approval"},
        ),
    ]
    expected = [
        "preparing_model",
        "waiting_model",
        "streaming_reasoning",
        "streaming_text",
        "streaming_tool_call",
        "preparing_tools",
        "preparing_tools",
        "preparing_tools",
        "preparing_tools",
        "waiting_approval",
    ]

    observed: list[str] = []
    for event in transitions:
        activity = reduce_runtime_activity(activity, event)
        observed.append(activity.activity)

    assert observed == expected
    assert activity.status == "waiting_approval"
    assert activity.run_phase == "human_interaction"
    assert activity.tool_calls[0].status == "awaiting_approval"

    for event in [
        _event(
            11,
            "approval_resolved",
            {
                "turn_id": "turn_1",
                "batch_id": "batch_1",
                "tool_call_id": "call_1",
                "tool_call_status": "ready",
            },
        ),
        _event(
            12,
            "tool_batch_started",
            {
                "turn_id": "turn_1",
                "batch_id": "batch_1",
                "tool_calls": [
                    {
                        "tool_call_id": "call_1",
                        "tool_name": "Read",
                        "status": "ready",
                        "source_index": 0,
                    }
                ],
            },
        ),
        _event(
            13,
            "tool_call_started",
            {
                "turn_id": "turn_1",
                "batch_id": "batch_1",
                "tool_call_id": "call_1",
                "tool_name": "Read",
                "source_index": 0,
            },
        ),
        _event(
            14,
            "tool_call_completed",
            {
                "turn_id": "turn_1",
                "batch_id": "batch_1",
                "tool_call_id": "call_1",
                "tool_name": "Read",
                "source_index": 0,
                "is_error": False,
                "result_data": {"line_count": 12},
            },
        ),
        _event(
            15,
            "tool_batch_results_committing",
            {"turn_id": "turn_1", "batch_id": "batch_1"},
        ),
    ]:
        activity = reduce_runtime_activity(activity, event)

    assert activity.status == "running"
    assert activity.activity == "committing_results"
    assert activity.run_phase == "result_commit"
    assert activity.batch_phase == "committing_results"
    tool = activity.tool_calls[0]
    assert tool.status == "completed"
    assert tool.arguments == {"path": "README.md"}
    assert tool.result_data == {"line_count": 12}
    assert tool.requested_at is not None
    assert tool.started_at is not None
    assert tool.ended_at is not None

    activity = reduce_runtime_activity(
        activity,
        _event(16, "tool_batch_completed", {"turn_id": "turn_1", "batch_id": "batch_1"}),
    )

    assert activity.activity == "preparing_model"
    assert activity.tool_calls == ()
    assert activity.batch_id is None


def test_runtime_activity_tracks_provider_retry_attempts_per_model_request() -> None:
    activity = reduce_runtime_activity(
        queued_runtime_activity("ses_1"),
        _event(1, "model_request_started", {"turn_id": "turn_1"}),
    )

    assert activity.activity == "waiting_model"
    assert activity.model_attempt is None
    assert activity.model_max_attempts is None

    second_attempt = reduce_runtime_activity(
        activity,
        _event(
            2,
            "run_retry_scheduled",
            {"turn_id": "turn_1", "attempt": 2, "max_attempts": 5},
        ),
    )
    third_attempt = reduce_runtime_activity(
        second_attempt,
        _event(
            3,
            "run_retry_scheduled",
            {"turn_id": "turn_1", "attempt": 3, "max_attempts": 5},
        ),
    )

    assert second_attempt.model_attempt == 2
    assert second_attempt.model_max_attempts == 5
    assert third_attempt.model_attempt == 3
    assert third_attempt.model_max_attempts == 5
    assert runtime_activity_changed(second_attempt, third_attempt) is True

    next_request = reduce_runtime_activity(
        third_attempt,
        _event(4, "model_request_started", {"turn_id": "turn_2"}),
    )
    assert next_request.model_attempt is None
    assert next_request.model_max_attempts is None


def test_runtime_activity_projection_overlays_authoritative_durable_tool_state() -> None:
    session = SessionRecord(session_id="ses_1", status="running", active_run_id="run_1")
    batch = ToolCallBatchRecord(
        batch_id="batch_1",
        turn_id="turn_1",
        assistant_message_id="msg_1",
        phase="executing",
        calls=[
            ToolBatchCall(
                request=ToolCallRequest(id="call_1", name="Read", source_index=0),
                status="running",
            )
        ],
    )
    run = RunRecord(
        session_id="ses_1",
        run_id="run_1",
        status="running",
        phase="tool_execution",
        active_tool_batch=batch,
    )
    snapshot = SessionSnapshot(
        session=session,
        runs=[run],
        events=[_event(1, "run_started")],
    )

    activity = project_runtime_activity(snapshot)

    assert activity.activity == "executing_tools"
    assert activity.run_phase == "tool_execution"
    assert activity.batch_phase == "executing"
    assert activity.tool_calls[0].status == "running"


def test_runtime_activity_does_not_project_afk_input_resolution_as_waiting_user() -> None:
    activity = queued_runtime_activity("ses_1")
    for event in [
        _event(1, "run_started"),
        _event(2, "tool_batch_planned", {"turn_id": "turn_1", "batch_id": "batch_1"}),
        _event(
            3,
            "tool_call_requested",
            {
                "turn_id": "turn_1",
                "batch_id": "batch_1",
                "tool_call_id": "call_1",
                "tool_name": "Workflow",
                "source_index": 0,
            },
        ),
        _event(
            4,
            "ask_user_requested",
            {
                "tool_call_id": "call_1",
                "interaction_mode": "unattended",
                "will_suspend": False,
            },
        ),
    ]:
        activity = reduce_runtime_activity(activity, event)

    assert activity.status == "running"
    assert activity.activity != "waiting_user"
    assert activity.tool_calls[0].status == "resolving_input"

    activity = reduce_runtime_activity(
        activity,
        _event(
            5,
            "ask_user_resolved",
            {"tool_call_id": "call_1", "tool_call_status": "completed"},
        ),
    )

    assert activity.tool_calls[0].status == "completed"


def test_runtime_activity_tracks_compaction_and_response_finalization() -> None:
    activity = queued_runtime_activity("ses_1")
    for event in [
        _event(1, "run_started"),
        _event(2, "compaction_started", {"compaction_id": "cmp_1"}),
    ]:
        activity = reduce_runtime_activity(activity, event)

    assert activity.activity == "compacting"
    assert activity.compaction_id == "cmp_1"

    for event in [
        _event(3, "compaction_completed", {"compaction_id": "cmp_1"}),
        _event(4, "model_request_started", {"turn_id": "turn_1"}),
        _event(5, "model_text_delta", {"turn_id": "turn_1"}),
        _event(
            6,
            "model_message_completed",
            {"turn_id": "turn_1", "tool_call_count": 0},
        ),
    ]:
        activity = reduce_runtime_activity(activity, event)

    assert activity.activity == "finalizing_response"
    assert activity.compaction_id is None

    activity = reduce_runtime_activity(activity, _event(7, "run_completed"))
    assert activity.activity == "completed"
    assert activity.status == "completed"


def test_runtime_activity_publishes_post_compaction_context_immediately() -> None:
    before = ContextMetrics(
        estimated_input_tokens=800,
        estimated_fixed_tokens=100,
        context_window_tokens=1_000,
        reserved_output_tokens=100,
        input_budget_tokens=900,
        estimated_remaining_tokens=100,
        estimated_utilization_ratio=800 / 900,
    )
    after = before.model_copy(
        update={
            "estimated_input_tokens": 250,
            "estimated_remaining_tokens": 650,
            "estimated_utilization_ratio": 250 / 900,
        }
    )
    activity = queued_runtime_activity("ses_1").model_copy(
        update={"effective_context_metrics": before}
    )

    activity = reduce_runtime_activity(
        activity,
        _event(
            1,
            "compaction_completed",
            {
                "compaction_id": "cmp_1",
                "context_metrics_after": after.model_dump(mode="json"),
            },
        ),
    )

    assert activity.effective_context_metrics == after


def test_repeated_stream_deltas_advance_cursor_without_republishing_activity() -> None:
    activity = reduce_runtime_activity(
        queued_runtime_activity("ses_1"),
        _event(1, "model_text_delta", {"turn_id": "turn_1", "text": "a"}),
    )
    next_activity = reduce_runtime_activity(
        activity,
        _event(2, "model_text_delta", {"turn_id": "turn_1", "text": "b"}),
    )

    assert next_activity.sequence == 2
    assert runtime_activity_changed(activity, next_activity) is False


def test_runtime_activity_tracks_live_context_estimate_and_provider_usage() -> None:
    activity = reduce_runtime_activity(
        queued_runtime_activity("ses_1"),
        _event(
            1,
            "model_request_started",
            {
                "context_id": "ctx_1",
                "provider": "test",
                "endpoint": "primary",
                "model": "test-model",
                "round": 1,
                "turn_id": "turn_1",
                "context_metrics": {
                    "estimated_input_tokens": 123,
                    "estimated_fixed_tokens": 10,
                    "context_window_tokens": 1_000,
                    "reserved_output_tokens": 100,
                    "input_budget_tokens": 900,
                    "estimated_remaining_tokens": 777,
                    "estimated_utilization_ratio": 123 / 900,
                    "over_budget": False,
                },
            },
        ),
    )

    assert activity.context_status is not None
    assert activity.context_status.context_id == "ctx_1"
    assert activity.context_status.metrics.estimated_input_tokens == 123

    completed = reduce_runtime_activity(
        activity,
        _event(
            2,
            "model_message_completed",
            {
                "context_id": "ctx_1",
                "turn_id": "turn_1",
                "tool_call_count": 0,
                "usage": {"input_tokens": 117, "output_tokens": 4},
            },
        ),
    )

    assert completed.context_status is not None
    assert completed.context_status.provider_reported_input_tokens == 117
    assert completed.context_status.completed_at is not None


def test_tool_events_preserve_exact_preflight_and_aborted_statuses() -> None:
    activity = reduce_runtime_activity(
        queued_runtime_activity("ses_1"),
        _event(
            1,
            "tool_batch_started",
            {
                "batch_id": "batch_1",
                "tool_calls": [
                    {
                        "tool_call_id": "call_1",
                        "tool_name": "Missing",
                        "status": "failed",
                        "source_index": 0,
                    }
                ],
            },
        ),
    )

    assert activity.tool_calls[0].status == "failed"

    activity = reduce_runtime_activity(
        activity,
        _event(
            2,
            "tool_call_completed",
            {
                "tool_call_id": "call_1",
                "tool_name": "Missing",
                "status": "aborted",
                "is_error": True,
                "source_index": 0,
            },
        ),
    )

    assert activity.tool_calls[0].status == "aborted"


def test_queued_activity_can_share_the_latest_durable_event_boundary() -> None:
    activity = queued_runtime_activity("ses_1", sequence=12)

    assert activity.sequence == 12
