from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ouro.core.errors import PersistenceError
from ouro.core.inputs import InputRecord, resolve_input_content
from ouro.core.messages import MessageFact, MessageRecord, ToolExecutionTiming, ToolResultReference
from ouro.core.persistence import decode_versioned_record_json
from ouro.core.tool_facts import ToolCallFacts, ToolCallIdentity, ToolCallRecord, ToolOutcomeRecord


def decode_message_content(payload: str | bytes, *, label: str) -> MessageRecord:
    """Decode owned content and fixed references once, before explicit resolution."""
    record = decode_versioned_record_json(MessageRecord, payload, label=label)
    try:
        return record.validate_persisted_content()
    except ValueError as exc:
        raise PersistenceError(f"could not validate {label}: {exc}") from exc


def validate_tool_message(
    fact: MessageRecord,
    calls: Mapping[ToolCallIdentity, ToolCallRecord],
    outcome: ToolOutcomeRecord | None,
) -> ToolExecutionTiming | None:
    """Check fixed references without projecting their parameters or observed bodies."""
    identities = list(fact.call_refs)
    if fact.outcome_ref is not None:
        identities.append(fact.outcome_ref.identity)
    for identity in identities:
        call = calls.get(identity)
        if call is None or call.session_id != fact.session_id:
            raise PersistenceError("message references a missing or foreign Call")
        if fact.run_id is not None and fact.run_id != identity.run_id:
            raise PersistenceError("message and Call belong to different Runs")
        if fact.turn_id is not None and fact.turn_id != call.turn_id:
            raise PersistenceError("message and Call belong to different turns")
    if fact.call_refs:
        ordered = [calls[identity] for identity in fact.call_refs]
        if any(call.producer.kind != "model" for call in ordered):
            raise PersistenceError("assistant message cannot contain Driver-origin Calls")
        if len({(call.identity.run_id, call.identity.batch_id) for call in ordered}) != 1:
            raise PersistenceError("assistant message references different native batches")
        if [call.request.source_index for call in ordered] != sorted(
            call.request.source_index for call in ordered
        ):
            raise PersistenceError("assistant Call references must preserve source order")
    reference = fact.outcome_ref
    if reference is not None and reference.outcome_attempt is not None:
        if outcome is None or outcome.attempt != reference.outcome_attempt:
            raise PersistenceError("message references a missing or different Outcome")
        try:
            outcome.validate_call(calls[reference.identity])
        except ValueError as exc:
            raise PersistenceError(str(exc)) from exc
        return ToolExecutionTiming(
            started_at=outcome.value.started_at,
            ended_at=outcome.value.ended_at or outcome.value.started_at,
        )
    return None


def resolve_tool_content(
    fact: MessageRecord,
    calls: Sequence[ToolCallRecord],
    outcome: ToolOutcomeRecord | None,
) -> dict[str, Any]:
    """Materialize fixed tool content only for an explicit message read."""
    selected = {call.identity: call for call in calls}
    timing = validate_tool_message(fact, selected, outcome)
    values: dict[str, Any] = {}
    if fact.call_refs:
        values["tool_calls"] = [selected[identity].request for identity in fact.call_refs]
    reference = fact.outcome_ref
    if reference is not None:
        call = selected[reference.identity]
        if reference.outcome_attempt is not None:
            assert outcome is not None
            result = outcome.value
            if fact.tool_feedback is not None:
                result = fact.tool_feedback.materialize(result)
            values["content"] = list(result.content_blocks)
            values["resource_refs"] = list(result.resource_refs)
            values["tool_execution"] = timing
            is_error = result.is_error
        else:
            assert reference.feedback_is_error is not None
            is_error = reference.feedback_is_error
        values["tool_result"] = ToolResultReference(
            tool_call_id=call.request.id,
            tool_name=call.request.name,
            producer=call.producer,
            is_error=is_error,
        )
    return values


def materialize_message(
    fact: MessageFact,
    record: InputRecord | None = None,
    *,
    calls: Sequence[ToolCallRecord] = (),
    outcome: ToolOutcomeRecord | None = None,
) -> MessageRecord:
    """Project a detached view when the caller retains ownership of source facts."""
    return materialize_owned_message(fact, record, calls=calls, outcome=outcome).model_copy(
        deep=True
    )


def materialize_owned_message(
    fact: MessageRecord,
    record: InputRecord | None = None,
    *,
    calls: Sequence[ToolCallRecord] = (),
    outcome: ToolOutcomeRecord | None = None,
) -> MessageRecord:
    """Transfer freshly decoded, privately owned facts into one mutable read result.

    Storage has already validated each typed fact. Bind and validate their
    cross-references without serializing or reparsing the owned message body.
    """
    if type(fact) is not MessageRecord:
        fact = MessageRecord.model_validate(vars(fact))
    if fact.input_ref is None and not fact.call_refs and fact.outcome_ref is None:
        return fact
    values = {
        **resolve_input_content(fact, record),
        **resolve_tool_content(fact, calls, outcome),
    }
    # Call/Outcome binding preserves the already checked message shape. An Input
    # can supply a new body for message-owned attachment offsets, so validate that
    # materialized view before returning it.
    vars(fact).update(values)
    fact.model_fields_set.update(values)
    return fact.validate_tool_fields() if fact.input_ref is not None else fact


def validate_tool_message_snapshot(messages: Sequence[MessageRecord], facts: ToolCallFacts) -> None:
    """Validate the references in an explicitly requested complete snapshot."""
    calls = {call.identity: call for call in facts.calls}
    outcomes = {(item.identity, item.attempt): item for item in facts.outcomes}
    for message in messages:
        reference = message.outcome_ref
        validate_tool_message(
            message,
            calls,
            outcomes.get((reference.identity, reference.outcome_attempt))
            if reference is not None and reference.outcome_attempt is not None
            else None,
        )
