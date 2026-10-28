from dataclasses import FrozenInstanceError, replace

import pytest
from pydantic import ValidationError

from ouro.core.session_commits import SessionCommit
from ouro.core.session_controls import SessionControlTransitionRequest
from ouro.core.state import SessionRecord
from ouro.core.tool_facts import (
    ToolCallFacts,
    ToolCallIdentity,
    ToolCallRecord,
    ToolCallStart,
    ToolOutcomeRecord,
)
from ouro.core.tools import ToolCallProducer, ToolCallRequest, ToolInvocation, ToolResult


def call_record():
    return ToolCallRecord(
        session_id="session",
        identity=ToolCallIdentity(run_id="run", batch_id="batch", tool_call_id="provider"),
        history_epoch=0,
        turn_id="turn",
        producer=ToolCallProducer(),
        request=ToolCallRequest(id="provider", name="tool", arguments={"nested": ["original"]}),
    )


def invocation(call):
    return ToolInvocation(
        session_id=call.session_id,
        run_id=call.identity.run_id,
        turn_id=call.turn_id,
        tool_call_id=call.identity.tool_call_id,
        tool_name=call.request.name,
        arguments=call.request.arguments,
    )


def test_call_identity_remains_an_immutable_serializable_key():
    call = call_record()
    decoded = ToolCallRecord.model_validate_json(call.model_dump_json())
    copied = call.model_copy(deep=True)
    assert copied.identity is call.identity
    identities = {call.identity: "accepted"}
    assert identities[decoded.identity] == identities[copied.identity] == "accepted"
    assert decoded.identity == call.identity
    assert replace(call.identity, batch_id="another") not in identities
    assert call.model_dump()["identity"] == {
        "run_id": "run",
        "batch_id": "batch",
        "tool_call_id": "provider",
    }
    with pytest.raises(FrozenInstanceError):
        call.identity.batch_id = "another"


@pytest.mark.parametrize("field", ["run_id", "batch_id", "tool_call_id"])
def test_call_identity_requires_nonempty_components(field):
    values = {"run_id": "run", "batch_id": "batch", "tool_call_id": "provider", field: ""}
    with pytest.raises(ValidationError):
        ToolCallIdentity(**values)
    call = call_record().model_dump()
    call["identity"] = values
    with pytest.raises(ValidationError):
        ToolCallRecord.model_validate(call)


def test_execution_binding_cannot_silently_discard_changed_arguments():
    call = call_record()
    actual = invocation(call).model_copy(update={"arguments": {"nested": ["approved"]}})
    with pytest.raises(ValidationError, match="from_invocation"):
        ToolCallStart(session_id=call.session_id, identity=call.identity, attempt=1, binding=actual)
    start = ToolCallStart.from_invocation(call, actual, attempt=1)
    actual.arguments["nested"].append("mutated")
    assert start.arguments_override == {"nested": ["approved"]}
    original = ToolCallStart.from_invocation(call, invocation(call), attempt=1)
    assert original.arguments_override is None
    materialized = original.materialize(call)
    materialized.arguments["nested"].append("mutated")
    assert call.request.arguments == {"nested": ["original"]}


def test_outcome_content_cannot_silently_drop_a_business_proposal():
    call = call_record()
    result = ToolResult(
        tool_call_id=call.identity.tool_call_id,
        tool_name=call.request.name,
        session_control_transitions=[
            SessionControlTransitionRequest(action="acquire", controller_id="mode")
        ],
    )
    with pytest.raises(ValidationError, match="business proposals"):
        ToolOutcomeRecord(
            session_id=call.session_id, identity=call.identity, attempt=1, value=result
        )


def test_outcome_preserves_structured_data_without_guessing_knowledge_state():
    call = call_record()
    result = ToolResult(
        tool_call_id=call.identity.tool_call_id,
        tool_name=call.request.name,
        content="observed bytes",
        structured={"outcome_unknown": True, "other": "tool-owned structured data"},
    )
    outcome = ToolOutcomeRecord(
        session_id=call.session_id, identity=call.identity, attempt=1, value=result
    )
    assert outcome.value.content == result.content
    assert outcome.value.structured == result.structured


def test_withdrawing_history_cannot_also_accept_or_start_a_call():
    call = call_record()
    session = SessionRecord(session_id=call.session_id)
    start = ToolCallStart.from_invocation(call, invocation(call), attempt=1)
    for proposed in (ToolCallFacts(calls=(call,)), ToolCallFacts(starts=(start,))):
        with pytest.raises(ValidationError, match="withdrawal cannot accept or start"):
            SessionCommit(before=session, before_message_id="msg_withdrawn", tool_facts=proposed)
