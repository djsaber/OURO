import pytest

from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord, TextContent
from ouro.core.state import RunRecord, SessionRecord


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


def test_event_round_trip() -> None:
    event = EventRecord(session_id="ses_1", run_id="run_1", event_type="run_started", sequence=1)

    loaded = EventRecord.model_validate_json(event.model_dump_json())

    assert loaded.event_type == "run_started"
    assert loaded.sequence == 1


def test_session_and_run_defaults() -> None:
    session = SessionRecord(title="Test")
    run = RunRecord(session_id=session.session_id)

    assert session.status == "idle"
    assert run.status == "queued"
    assert run.session_id == session.session_id
