from __future__ import annotations

from ouro.core.context_snapshots import ModelRequestHeader
from ouro.core.events import EventRecord
from ouro.core.response_drafts import ResponseStreamPosition
from ouro.sessions import SessionRepository


def read_draft_events(
    store: SessionRepository,
    session_id: str,
    *,
    sequence: int,
    header: ModelRequestHeader | None,
    history_epoch: int,
    after: ResponseStreamPosition | None = None,
    limit: int | None = None,
) -> tuple[EventRecord, ...]:
    """Read current output inside the caller's Session read boundary."""
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
