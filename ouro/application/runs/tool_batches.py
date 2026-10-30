from __future__ import annotations

from copy import deepcopy
from typing import cast

from ouro.core.content import ContentPart
from ouro.core.errors import PersistenceError
from ouro.core.state import RunRecord
from ouro.core.tool_batches import (
    ToolBatchCall,
    ToolCallBatchRecord,
    ToolCallState,
    ToolResultProposals,
    validate_call_states,
)
from ouro.core.tool_facts import (
    ToolCallFacts,
    ToolCallIdentity,
    ToolCallRecord,
    ToolCallStart,
    ToolFactConflict,
    ToolOutcomeRecord,
)
from ouro.core.tools import ToolInvocation, ToolInvocationBinding, ToolResult, ToolResultValue
from ouro.sessions.base import SessionRepository


def load_batch_states(store: SessionRepository, run: RunRecord) -> tuple[ToolCallState, ...]:
    """Read current per-call metadata without original parameters or result bodies."""
    head = run.active_tool_batch
    if head is None:
        return ()
    with store.session_read(run.session_id):
        states = tuple(
            store.load_tool_call_state(run.session_id, head.identity(run.run_id, call_id))
            for call_id in head.call_ids
        )
        if any(state is None for state in states):
            raise PersistenceError("active batch references a missing Call state")
        return tuple(state for state in states if state is not None)


def load_tool_batch(store: SessionRepository, run: RunRecord) -> ToolCallBatchRecord | None:
    """Explicitly read the selected batch; the Run itself never materializes bodies."""
    head = run.active_tool_batch
    if head is None:
        return None
    with store.session_read(run.session_id):
        calls = [
            load_batch_call(store, run.session_id, head.identity(run.run_id, call_id))
            for call_id in head.call_ids
        ]
    return ToolCallBatchRecord(**head.model_dump(exclude={"call_ids"}), calls=calls)


def load_batch_call(
    store: SessionRepository,
    session_id: str,
    identity: ToolCallIdentity,
    *,
    state: ToolCallState | None = None,
) -> ToolBatchCall:
    call = store.load_tool_call(session_id, identity)
    if state is None:
        state = store.load_tool_call_state(session_id, identity)
    if call is None or state is None:
        raise PersistenceError("active batch references a missing Call or Call state")
    start = (
        store.load_tool_call_start(session_id, identity, state.attempt)
        if state.attempt > 0
        else None
    )
    if state.attempt > 0 and start is None:
        raise PersistenceError("Call state references a missing start")
    invocation = start.materialize(call) if start is not None else None
    if state.prepared_binding is not None:
        invocation = ToolInvocation.model_validate(
            {
                **state.prepared_binding.model_dump(),
                "arguments": deepcopy(
                    state.arguments_override
                    if state.arguments_override is not None
                    else call.request.arguments
                ),
            }
        )
    if invocation is not None and state.interaction_activation_id is not None:
        invocation = invocation.model_copy(
            update={"interaction_activation_id": state.interaction_activation_id}
        )
    approval = state.approval
    if approval is not None and approval.status == "modified" and invocation is not None:
        approval = approval.model_copy(
            update={"modified_arguments": deepcopy(invocation.arguments)}
        )
    result = None
    outcome = None
    feedback = state.feedback
    if state.outcome_attempt is not None:
        if state.result_message_id is not None:
            message_fact = store.load_message_fact(session_id, state.result_message_id)
            if (
                message_fact.outcome_ref is None
                or message_fact.outcome_ref.identity != identity
                or message_fact.outcome_ref.outcome_attempt != state.outcome_attempt
            ):
                raise PersistenceError("published Call does not match its fixed message reference")
            feedback = message_fact.tool_feedback
        outcome = store.load_tool_outcome(session_id, identity, state.outcome_attempt)
        if outcome is None:
            raise PersistenceError("Call state references a missing Outcome")
        try:
            outcome.validate_call(call)
        except ValueError as exc:
            raise PersistenceError(str(exc)) from exc
        result = ToolResult.model_validate(
            {
                **outcome.value.model_dump(),
                **(state.proposals.model_dump() if state.proposals is not None else {}),
            }
        )
        if feedback is not None:
            result = feedback.materialize(result)
    elif state.result_message_id is not None:
        message = store.load_message(session_id, state.result_message_id)
        if message is None or message.tool_result is None:
            raise PersistenceError("Call state references a missing feedback message")
        result = ToolResult(
            tool_call_id=identity.tool_call_id,
            tool_name=call.request.name,
            content=cast(list[ContentPart], list(message.content)),
            is_error=message.tool_result.is_error,
            resource_refs=list(message.resource_refs),
            ended_at=message.created_at,
            structured={
                "outcome_unknown": state.status == "unknown_outcome",
                "recovered_interruption": True,
            },
        )
    validate_call_states(
        (state,),
        ToolCallFacts(
            calls=(call,),
            starts=(start,) if start is not None else (),
            outcomes=(outcome,) if outcome is not None else (),
        ),
        session_id=session_id,
    )
    return ToolBatchCall(
        request=call.request,
        status=state.status,
        invocation=invocation,
        started_at=start.started_at if start is not None else None,
        approval=approval,
        input_request=state.input_request,
        input_resolution=state.input_resolution,
        input_revision=state.input_revision,
        agent_wait=state.agent_wait,
        result=result,
        feedback=feedback,
        result_message_id=state.result_message_id,
        attempt=state.attempt,
        metadata=state.metadata,
    )


def prepare_batch_changes(
    store: SessionRepository,
    run: RunRecord,
    batch: ToolCallBatchRecord,
    *,
    changed_call_ids: frozenset[str] | None = None,
    started_call_ids: frozenset[str] = frozenset(),
    accept_proposals: bool = True,
) -> tuple[ToolCallBatchRecord, ToolCallFacts, tuple[ToolCallState, ...]]:
    """Prepare only selected Call changes inside the caller's mutation boundary."""
    accepted = []
    starts = []
    outcomes = []
    states = []
    materialized = []
    for item in batch.calls:
        if changed_call_ids is not None and item.request.id not in changed_call_ids:
            materialized.append(item)
            continue
        identity = ToolCallIdentity(
            run_id=run.run_id, batch_id=batch.batch_id, tool_call_id=item.request.id
        )
        call = store.load_tool_call(run.session_id, identity)
        if call is None:
            call = ToolCallRecord(
                session_id=run.session_id,
                identity=identity,
                history_epoch=run.history_epoch,
                turn_id=batch.turn_id,
                producer=batch.producer,
                request=item.request,
                accepted_at=batch.created_at,
            )
            accepted.append(call)
        elif call.request != item.request or call.producer != batch.producer:
            raise ToolFactConflict("accepted Call request cannot be replaced")
        previous = store.load_tool_call_state(run.session_id, identity)
        attempt = previous.attempt if previous is not None else 0
        if item.request.id in started_call_ids:
            if item.invocation is None:
                raise ValueError("starting a Call requires its actual invocation")
            attempt += 1
            starts.append(ToolCallStart.from_invocation(call, item.invocation, attempt=attempt))
        item = item.model_copy(update={"attempt": attempt})
        if previous is not None and previous.attempt == attempt and previous.agent_wait is not None:
            wait = previous.agent_wait
            proposed = item.agent_wait
            if proposed is not None and (
                proposed.wait_id != wait.wait_id
                or proposed.delegation_id != wait.delegation_id
                or proposed.child_run_id != wait.child_run_id
            ):
                raise ToolFactConflict("a started Call cannot replace its Agent wait binding")
            # The Agent service binds before child dispatch, while the executor
            # still holds its pre-dispatch batch. Cancellation must retain that
            # binding and cannot reopen a wait already resolved by settlement.
            if proposed is None or wait.state == "resolved":
                item = item.model_copy(update={"agent_wait": wait})
        materialized.append(item)
        outcome_attempt = previous.outcome_attempt if previous is not None else None
        proposals = None
        if item.result is not None and item.status != "unknown_outcome":
            value = ToolResultValue.model_validate(
                item.result.model_dump(exclude=set(ToolResultProposals.model_fields))
            )
            outcome = store.load_tool_outcome(run.session_id, identity, attempt)
            if outcome is None:
                if item.feedback is not None:
                    raise ToolFactConflict("settlement feedback requires an already saved Outcome")
                outcome = ToolOutcomeRecord(
                    session_id=run.session_id, identity=identity, attempt=attempt, value=value
                )
                outcomes.append(outcome)
            elif not (
                item.feedback.materialize(outcome.value) if item.feedback else outcome.value
            ).same_observation(value):
                raise ToolFactConflict("observed Outcome cannot be replaced by a batch update")
            outcome_attempt = attempt
            if item.result_message_id is None and accept_proposals:
                proposals = ToolResultProposals.from_result(item.result)
        prepared_binding = None
        arguments_override = None
        if item.invocation is not None and (
            attempt == 0 or item.status in {"ready", "awaiting_approval"}
        ):
            prepared_binding = ToolInvocationBinding.model_validate(
                item.invocation.model_dump(exclude={"arguments"})
            )
            if item.invocation.arguments != call.request.arguments:
                arguments_override = deepcopy(item.invocation.arguments)
        state = ToolCallState(
            session_id=run.session_id,
            identity=identity,
            status=item.status,
            attempt=attempt,
            prepared_binding=prepared_binding,
            arguments_override=arguments_override,
            approval=(
                item.approval.model_copy(update={"modified_arguments": None})
                if item.approval is not None
                else None
            ),
            input_request=item.input_request,
            input_resolution=item.input_resolution,
            input_revision=item.input_revision,
            interaction_activation_id=(
                item.invocation.interaction_activation_id if item.invocation is not None else None
            ),
            agent_wait=item.agent_wait,
            outcome_attempt=outcome_attempt,
            proposals=proposals,
            feedback=item.feedback if item.result_message_id is None else None,
            result_message_id=item.result_message_id,
            metadata=item.metadata,
        )
        if previous != state:
            states.append(state)
    return (
        batch.model_copy(update={"calls": materialized}),
        ToolCallFacts(calls=tuple(accepted), starts=tuple(starts), outcomes=tuple(outcomes)),
        tuple(states),
    )
