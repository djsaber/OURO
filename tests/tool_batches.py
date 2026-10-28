from ouro.application.contracts.agents import SpawnAgentRequest
from ouro.application.runs.tool_batches import prepare_batch_changes
from ouro.core.message_content import materialize_message
from ouro.core.messages import MessageRecord, ToolMessageReference
from ouro.core.session_commits import SessionCommit
from ouro.core.state import RunRecord
from ouro.core.tool_batches import ToolBatchCall, ToolCallBatchRecord
from ouro.core.tools import ToolCallRequest, ToolInvocation, ToolResult
from ouro.sessions.base import SessionRepository


def seed_spawn_request(store: SessionRepository, request: SpawnAgentRequest) -> SpawnAgentRequest:
    """Give service-level model delegation fixtures an accepted, started Call."""
    assert request.parent_batch_id is not None and request.parent_tool_call_id is not None
    request = request.model_copy(
        update={
            "request_id": (
                f"{request.parent_run_id}/{request.parent_batch_id}/{request.parent_tool_call_id}"
            )
        }
    )
    run = store.load_run(request.parent_session_id, request.parent_run_id)
    batch = ToolCallBatchRecord(
        batch_id=request.parent_batch_id,
        turn_id="turn_spawn",
        assistant_message_id="msg_spawn",
        phase="executing",
        calls=[
            ToolBatchCall(
                request=ToolCallRequest(
                    id=request.parent_tool_call_id,
                    name="SpawnAgent",
                    arguments=request.model_dump(
                        include={
                            "agent_id",
                            "task",
                            "description",
                            "nickname",
                            "execution_mode",
                            "resource_refs",
                        }
                    ),
                ),
                status="running",
            )
        ],
    )
    persist_tool_batch(store, run, batch)
    return request


def persist_tool_batch(
    store: SessionRepository, run: RunRecord, batch: ToolCallBatchRecord
) -> RunRecord:
    """Seed an accepted batch at a chosen execution boundary for recovery tests."""
    started_ids = frozenset(
        call.request.id
        for call in batch.calls
        if call.status
        in {
            "running",
            "completed",
            "unknown_outcome",
            "resolving_input",
            "waiting_user",
            "waiting_child",
        }
    )
    batch = batch.model_copy(
        update={
            "calls": [
                call.model_copy(
                    update={
                        "invocation": call.invocation
                        or ToolInvocation(
                            session_id=run.session_id,
                            run_id=run.run_id,
                            turn_id=batch.turn_id,
                            tool_call_id=call.request.id,
                            tool_name=call.request.name,
                            arguments=call.request.arguments,
                            producer=batch.producer,
                        )
                    }
                )
                if call.request.id in started_ids
                else call
                for call in batch.calls
            ]
        }
    )
    accepting_run = run.model_copy(update={"status": "running", "active_tool_batch": batch.head()})
    with store.session_mutation(run.session_id):
        before = store.load_session_record(run.session_id)
        _, facts, states = prepare_batch_changes(
            store, accepting_run, batch, started_call_ids=started_ids
        )
        committed = store.commit_session(
            SessionCommit(
                before=before,
                after=before.model_copy(update={"active_run_id": run.run_id, "status": "running"}),
                runs=(accepting_run,),
                tool_facts=facts,
                call_states=states,
            )
        )
        final_run = run.model_copy(update={"active_tool_batch": batch.head()})
        store.save_run(final_run)
        store.save_session(
            committed.session.model_copy(
                update={"active_run_id": before.active_run_id, "status": before.status}
            )
        )
    return final_run


def resolved_tool_batch(run: RunRecord, results: list[ToolResult]) -> ToolCallBatchRecord:
    """Construct a complete working frame for native publication boundary tests."""
    return ToolCallBatchRecord(
        turn_id="turn_result",
        assistant_message_id="msg_request",
        phase="committing_results",
        calls=[
            ToolBatchCall(
                request=ToolCallRequest(
                    id=result.tool_call_id, name=result.tool_name, source_index=index
                ),
                status="completed",
                result=result,
            )
            for index, result in enumerate(results)
        ],
    )


def bind_tool_messages(store, run, batch, messages):
    """Bind a fixture's explicit native batch to its materialized history messages."""
    for message in messages:
        if message.tool_calls:
            message.call_refs = tuple(
                batch.head().identity(run.run_id, call.id) for call in message.tool_calls
            )
        if message.tool_result is not None:
            identity = batch.head().identity(run.run_id, message.tool_result.tool_call_id)
            state = store.load_tool_call_state(run.session_id, identity)
            message.outcome_ref = ToolMessageReference(
                identity=identity,
                outcome_attempt=state.outcome_attempt,
                feedback_is_error=message.tool_result.is_error
                if state.outcome_attempt is None
                else None,
            )
        calls = [
            store.load_tool_call(run.session_id, identity)
            for identity in message.referenced_call_ids
        ]
        outcome = (
            store.load_tool_outcome(
                run.session_id, message.outcome_ref.identity, message.outcome_ref.outcome_attempt
            )
            if message.outcome_ref is not None and message.outcome_ref.outcome_attempt is not None
            else None
        )
        resolved = materialize_message(message.as_fact(), calls=calls, outcome=outcome)
        for field in ("content", "tool_calls", "tool_result", "tool_execution", "resource_refs"):
            setattr(message, field, getattr(resolved, field))


def seed_tool_messages(store, assistant: MessageRecord, results: list[MessageRecord], *, run=None):
    """Build accepted facts for history fixtures without executing a synthetic handler."""
    if run is None:
        values = {"run_id": assistant.run_id} if assistant.run_id else {}
        run = RunRecord(session_id=assistant.session_id, status="completed", **values)
    outputs = {message.tool_result.tool_call_id: message for message in results}
    calls = []
    for request in assistant.tool_calls:
        message = outputs.get(request.id)
        result = (
            ToolResult(
                tool_call_id=request.id,
                tool_name=request.name,
                content=message.content,
                is_error=message.tool_result.is_error,
                resource_refs=message.resource_refs,
                started_at=message.created_at,
                ended_at=message.created_at,
            )
            if message is not None
            else None
        )
        calls.append(
            ToolBatchCall(
                request=request, status="completed" if result else "aborted", result=result
            )
        )
    batch = ToolCallBatchRecord(
        turn_id=assistant.turn_id or "turn_history",
        assistant_message_id=assistant.message_id,
        phase="completed",
        calls=calls,
    )
    persist_tool_batch(store, run, batch)
    bind_tool_messages(store, run, batch, [assistant, *results])
    store.save_run(run)
    return run, batch
