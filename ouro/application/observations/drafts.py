from __future__ import annotations

from ouro.core.errors import PersistenceError
from ouro.core.events import EventRecord
from ouro.core.response_drafts import ResponseStreamPosition
from ouro.core.tool_facts import ToolCallIdentity
from ouro.core.tools import ToolResultFeedback
from ouro.sessions import SessionRepository


def read_draft_events(
    store: SessionRepository,
    session_id: str,
    *,
    sequence: int,
    history_epoch: int,
    after: ResponseStreamPosition | None = None,
    limit: int | None = None,
) -> tuple[EventRecord, ...]:
    """Read current output inside the caller's Session read boundary."""
    header = store.load_latest_context_header(session_id)
    if header is None or header.response_closed_at is not None:
        return ()
    cursor = (
        (after.cursor or 0) if after is not None and after.context_id == header.context_id else 0
    )
    page = store.load_response_draft(
        session_id, header.context_id, after_cursor=cursor, limit=limit
    )
    if page is None:
        return ()
    events = []
    for chunk in page.chunks:
        events.append(
            EventRecord(
                session_id=session_id,
                run_id=header.run_id,
                turn_id=header.turn_id,
                history_epoch=history_epoch,
                sequence=sequence,
                created_at=header.started_at or header.prepared_at,
                event_type="model_text_delta"
                if chunk.channel == "text"
                else "model_reasoning_delta",
                payload={
                    "text" if chunk.channel == "text" else "reasoning_content": chunk.content,
                    "turn_id": header.turn_id,
                },
                stream=ResponseStreamPosition(
                    context_id=header.context_id, after_cursor=cursor, cursor=chunk.cursor
                ),
            )
        )
        cursor = chunk.cursor
    return tuple(events)


def resolve_fact_event(
    store: SessionRepository, event: EventRecord, *, history_epoch: int
) -> EventRecord:
    """Resolve fixed content references inside the caller's read boundary."""
    if event.event_type not in {
        "model_response_committed",
        "tool_call_requested",
        "tool_call_completed",
    }:
        return event
    if event.history_epoch != history_epoch:
        return event
    payload = dict(event.payload)
    if event.event_type == "model_response_committed":
        identity = payload.get("message_id")
        if not isinstance(identity, str):
            return event
        message = store.load_message(event.session_id, identity)
        payload.update(text=message.text_content, reasoning_content=message.reasoning_content)
    else:
        batch_id, call_id = payload.get("batch_id"), payload.get("tool_call_id")
        if event.run_id is None or not isinstance(batch_id, str) or not isinstance(call_id, str):
            return event
        call_identity = ToolCallIdentity(
            run_id=event.run_id, batch_id=batch_id, tool_call_id=call_id
        )
        if event.event_type == "tool_call_requested":
            call = store.load_tool_call(event.session_id, call_identity)
            if call is None:
                raise PersistenceError("tool event references a missing Call")
            payload["arguments"] = call.request.arguments
        else:
            attempt = payload.get("outcome_attempt")
            if not isinstance(attempt, int):
                return event
            outcome = store.load_tool_outcome(event.session_id, call_identity, attempt)
            if outcome is None:
                raise PersistenceError("tool event references a missing Outcome")
            value = outcome.value
            if payload.get("feedback") is not None:
                value = ToolResultFeedback.model_validate(payload["feedback"]).materialize(value)
            payload["result_data"] = value.structured
    return event.model_copy(update={"payload": payload})
