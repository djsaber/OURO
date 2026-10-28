import pytest

from ouro.core.content import ImageContent, TextContent
from ouro.core.errors import PersistenceError
from ouro.core.message_content import materialize_message
from ouro.core.messages import MessageFact, MessageRecord, ToolMessageReference
from ouro.core.tool_facts import ToolCallIdentity, ToolCallRecord, ToolOutcomeRecord
from ouro.core.tools import ToolCallProducer, ToolCallRequest, ToolResultFeedback, ToolResultValue


def source_facts():
    identity = ToolCallIdentity(run_id="run_one", batch_id="batch_one", tool_call_id="call_one")
    call = ToolCallRecord(
        session_id="session_one",
        identity=identity,
        turn_id="turn_one",
        history_epoch=0,
        producer=ToolCallProducer(),
        request=ToolCallRequest(
            id=identity.tool_call_id, name="Draw", arguments={"items": ["one"]}
        ),
    )
    outcome = ToolOutcomeRecord(
        session_id=call.session_id,
        identity=identity,
        attempt=1,
        value=ToolResultValue(
            tool_call_id=identity.tool_call_id,
            tool_name=call.request.name,
            content=[TextContent(text="drawn"), ImageContent(uri="data:image/png;base64,aW1hZ2U=")],
            structured={"separate": "provider-facing text is not structured data"},
            resource_refs=["resource_one"],
        ),
    )
    return call, outcome


def test_tool_message_views_keep_call_and_outcome_ownership():
    call, outcome = source_facts()
    assistant = MessageFact(
        session_id=call.session_id, role="assistant", call_refs=(call.identity,)
    )
    result = MessageFact(
        session_id=call.session_id,
        role="tool_result",
        outcome_ref=ToolMessageReference(identity=call.identity, outcome_attempt=1),
    )
    original_call, original_outcome = call.model_dump_json(), outcome.model_dump_json()
    assistant_view = materialize_message(assistant, calls=[call])
    result_view = materialize_message(result, calls=[call], outcome=outcome)
    assert assistant_view.tool_calls == [call.request]
    assert result_view.content == outcome.value.content
    assert result_view.resource_refs == outcome.value.resource_refs
    assert result_view.tool_result.tool_name == "Draw"
    assert assistant_view.as_fact() == assistant
    assert result_view.as_fact() == result
    assert "drawn" not in result_view.as_fact().model_dump_json()
    assistant_view.tool_calls[0].arguments["items"].append("local")
    result_view.content[0].text = "local"
    assert call.model_dump_json() == original_call
    assert outcome.model_dump_json() == original_outcome


def test_literal_unknown_feedback_does_not_follow_a_late_outcome():
    call, outcome = source_facts()
    feedback = MessageFact(
        session_id=call.session_id,
        role="tool_result",
        content=[TextContent(text="Outcome unknown at publication")],
        outcome_ref=ToolMessageReference(identity=call.identity, feedback_is_error=True),
    )
    view = materialize_message(feedback, calls=[call], outcome=outcome)
    assert view.text_content == "Outcome unknown at publication"
    assert view.tool_result.is_error
    assert view.as_fact() == feedback


def test_framework_feedback_preserves_original_observation_and_media():
    call, outcome = source_facts()
    original = outcome.model_dump_json()
    feedback = ToolResultFeedback(
        error_type="ToolBusinessEffectsNotApplied", error_message="State changes were discarded."
    )
    fact = MessageFact(
        session_id=call.session_id,
        role="tool_result",
        outcome_ref=ToolMessageReference(identity=call.identity, outcome_attempt=1),
        tool_feedback=feedback,
    )
    message = materialize_message(fact, calls=[call], outcome=outcome)
    assert message.content[:2] == outcome.value.content
    assert message.text_content == "drawn\nState changes were discarded."
    assert message.tool_result.is_error
    assert message.resource_refs == ["resource_one"]
    assert message.as_fact() == fact
    assert "drawn" not in fact.model_dump_json()
    assert outcome.model_dump_json() == original


@pytest.mark.parametrize("literal", [False, True])
def test_settlement_feedback_requires_a_fixed_observation(literal):
    call, _ = source_facts()
    with pytest.raises(ValueError, match="feedback requires"):
        MessageFact(
            session_id=call.session_id,
            role="tool_result" if literal else "user",
            outcome_ref=(
                ToolMessageReference(identity=call.identity, feedback_is_error=True)
                if literal
                else None
            ),
            tool_feedback=ToolResultFeedback(error_type="Failure", error_message="Details"),
        )


@pytest.mark.parametrize("damage", ["missing_call", "session", "run", "turn", "attempt", "tool"])
def test_tool_reference_rejects_missing_or_foreign_facts(damage):
    call, outcome = source_facts()
    message = MessageFact(
        session_id=call.session_id,
        run_id=call.identity.run_id,
        turn_id=call.turn_id,
        role="tool_result",
        outcome_ref=ToolMessageReference(identity=call.identity, outcome_attempt=1),
    )
    if damage in {"session", "run", "turn"}:
        message = message.model_copy(update={f"{damage}_id": "other"})
    elif damage == "attempt":
        outcome = outcome.model_copy(update={"attempt": 2})
    elif damage == "tool":
        outcome = outcome.model_copy(
            update={"value": outcome.value.model_copy(update={"tool_name": "Other"})}
        )
    with pytest.raises(PersistenceError):
        materialize_message(
            message, calls=[] if damage == "missing_call" else [call], outcome=outcome
        )


def test_persistence_rejects_inline_tool_parameters_and_duplicate_outcome_content():
    call, _ = source_facts()
    with pytest.raises(ValueError, match="Call facts"):
        MessageRecord(
            session_id=call.session_id, role="assistant", tool_calls=[call.request]
        ).as_fact()
    with pytest.raises(ValueError, match="observed content"):
        MessageFact(
            session_id=call.session_id,
            role="tool_result",
            content=[TextContent(text="copied")],
            outcome_ref=ToolMessageReference(identity=call.identity, outcome_attempt=1),
        )
