from datetime import timedelta

import pytest

from ouro.core.events import EventRecord
from ouro.core.messages import (
    MessageRecord,
    ModelMessage,
    ModelResponseRecord,
    TextContent,
    ToolExecutionTiming,
    ToolResultReference,
)


def test_message_round_trip() -> None:
    message = MessageRecord.text(session_id="ses_1", run_id="run_1", role="user", text="hello")

    loaded = MessageRecord.model_validate_json(message.model_dump_json())

    assert loaded.session_id == "ses_1"
    assert loaded.text_content == "hello"
    assert isinstance(loaded.content[0], TextContent)


def test_message_turn_id_requires_run_id() -> None:
    with pytest.raises(ValueError, match="turn_id requires run_id"):
        MessageRecord.text(
            session_id="ses_1",
            role="assistant",
            text="hello",
            turn_id="turn_1",
        )


def test_message_rejects_tool_fields_in_metadata() -> None:
    with pytest.raises(ValueError, match="typed message fields"):
        MessageRecord.model_validate(
            {
                "session_id": "ses_1",
                "role": "assistant",
                "metadata": {"tool_calls": [{"id": "call_1", "name": "Read"}]},
            }
        )


def test_message_rejects_reasoning_state_in_metadata() -> None:
    with pytest.raises(ValueError, match="typed message fields"):
        MessageRecord.model_validate(
            {
                "session_id": "ses_1",
                "role": "assistant",
                "metadata": {"reasoning_content": "private reasoning"},
            }
        )


def test_model_response_state_belongs_only_to_assistant_messages() -> None:
    now = EventRecord(session_id="ses_time", event_type="diagnostic").created_at
    response = ModelResponseRecord(
        provider_id="provider",
        endpoint_id="chat",
        model_id="model",
        started_at=now,
        ended_at=now,
        reasoning_ended_at=now,
    )

    assistant = MessageRecord.text(
        session_id="ses_1",
        run_id="run_1",
        role="assistant",
        text="done",
    ).model_copy(update={"model_response": response})

    assert MessageRecord.model_validate_json(assistant.model_dump_json()).model_response == response
    with pytest.raises(ValueError, match="only assistant messages"):
        MessageRecord(
            session_id="ses_1",
            role="user",
            model_response=response,
        )


def test_tool_execution_timing_is_durable_but_not_model_visible() -> None:
    started_at = EventRecord(session_id="ses_time", event_type="diagnostic").created_at
    timing = ToolExecutionTiming(
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=3),
    )
    message = MessageRecord(
        session_id="ses_1",
        role="tool_result",
        tool_result=ToolResultReference(tool_call_id="call_1", tool_name="Read"),
        tool_execution=timing,
    )

    loaded = MessageRecord.model_validate_json(message.model_dump_json())

    assert loaded.tool_execution == timing
    assert "tool_execution" not in ModelMessage.from_record(loaded).model_dump()
    with pytest.raises(ValueError, match="cannot precede"):
        ToolExecutionTiming(
            started_at=timing.ended_at,
            ended_at=timing.started_at,
        )
