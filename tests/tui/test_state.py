from __future__ import annotations

from ouro.core.events import EventRecord
from ouro.tui.state import TuiSessionProjection, question_text


def _event(sequence: int, event_type: str, **payload: object) -> EventRecord:
    return EventRecord(
        session_id="ses_test",
        run_id="run_test",
        event_type=event_type,
        sequence=sequence,
        payload=payload,
    )


def test_projection_accumulates_stream_and_ignores_duplicate_events() -> None:
    projection = TuiSessionProjection("ses_test")

    projection.apply(_event(1, "run_started", trigger="user"))
    first = projection.apply(_event(2, "model_text_delta", text="Hello "))
    second_event = _event(3, "model_text_delta", text="**OURO**")
    second = projection.apply(second_event)
    duplicate = projection.apply(second_event)
    projection.apply(_event(4, "run_completed"))

    assert first is not None and first.assistant_delta == "Hello "
    assert second is not None and second.assistant_delta == "**OURO**"
    assert duplicate is None
    assert projection.assistant_text == "Hello **OURO**"
    assert projection.activity.status == "completed"


def test_projection_tracks_tool_lifecycle_and_approval() -> None:
    projection = TuiSessionProjection("ses_test")
    projection.apply(_event(1, "run_started", trigger="user"))
    projection.apply(_event(2, "tool_batch_planned", batch_id="batch_1"))
    projection.apply(
        _event(
            3,
            "tool_call_requested",
            tool_call_id="call_1",
            tool_name="Write",
            source_index=0,
        )
    )
    requested = projection.apply(
        _event(
            4,
            "approval_requested",
            approval_id="approval_1",
            tool_call_id="call_1",
            tool_name="Write",
        )
    )

    assert requested is not None
    assert requested.pending_interaction is not None
    assert requested.pending_interaction.kind == "approval"
    assert projection.activity.tool_calls[0].status == "awaiting_approval"

    projection.apply(
        _event(
            5,
            "approval_resolved",
            approval_id="approval_1",
            tool_call_id="call_1",
            tool_call_status="ready",
        )
    )
    projection.apply(
        _event(
            6,
            "tool_call_started",
            tool_call_id="call_1",
            tool_name="Write",
            source_index=0,
        )
    )
    projection.apply(
        _event(
            7,
            "tool_call_completed",
            tool_call_id="call_1",
            tool_name="Write",
            status="completed",
            source_index=0,
        )
    )

    assert projection.pending_interaction is None
    assert projection.activity.tool_calls[0].status == "completed"


def test_projection_exposes_structured_user_question() -> None:
    projection = TuiSessionProjection("ses_test")

    update = projection.apply(
        _event(
            1,
            "ask_user_requested",
            tool_call_id="call_ask",
            tool_name="AskUser",
            request={"question": "Which environment should OURO use?"},
        )
    )

    assert update is not None and update.pending_interaction is not None
    assert update.pending_interaction.kind == "question"
    assert question_text(update.pending_interaction) == "Which environment should OURO use?"


def test_projection_rejects_other_session_events() -> None:
    projection = TuiSessionProjection("ses_test")
    event = EventRecord(
        session_id="ses_other",
        run_id="run_other",
        event_type="model_text_delta",
        sequence=1,
        payload={"text": "not ours"},
    )

    assert projection.apply(event) is None
    assert projection.assistant_text == ""


def test_projection_exposes_live_event_delivery_failure() -> None:
    projection = TuiSessionProjection("ses_test")

    update = projection.apply(
        _event(
            1,
            "event_sink_failed",
            source_event_type="model_text_delta",
            error_type="TimeoutError",
            error_message="event sink timed out",
        )
    )

    assert update is not None
    assert update.diagnostic == "Live event delivery failed: event sink timed out"
