"""Freeze published source evidence and deliver it with its owning user input."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from itertools import chain

from ouro.application.sessions.history import published_history
from ouro.core.context import TokenEstimator, estimate_message_tokens, estimate_text_tokens
from ouro.core.context_selection import MessageBoundary
from ouro.core.errors import SessionBranchError
from ouro.core.events import EventRecord
from ouro.core.inputs import HistoryExcerpt, SourceHistory
from ouro.core.messages import MessageRecord, TextContent
from ouro.core.session_ports import SessionRepository
from ouro.core.state import SessionRecord


def _delivery_key(target: str, source: str) -> str:
    return f"ouro.source-history/{target}/{source}"


def _delivered_cursor(
    store: SessionRepository,
    target: SessionRecord,
    key: str,
    preceding: Sequence[MessageRecord] = (),
) -> tuple[int, MessageBoundary | None]:
    receipt = next(
        (m for m in reversed(preceding) if m.metadata.get("context_provider_delivery_key") == key),
        None,
    ) or store.find_context_provider_message(target.session_id, key)
    metadata = (receipt or target).metadata
    boundary = metadata["source_boundary"]
    cursor = MessageBoundary.model_validate(boundary) if boundary is not None else None
    return metadata["source_history_epoch"], cursor


def _source_messages(
    store: SessionRepository, source_id: str, after: int
) -> Iterator[MessageRecord]:
    while True:
        page, more = store.load_messages_after(source_id, after_sequence=after, limit=64)
        yield from page
        if not more:
            return
        after = page[-1].sequence


def _source_frames(messages: Iterable[MessageRecord]) -> Iterator[list[MessageRecord]]:
    """Keep a tool call and its published results together across page boundaries."""
    frame: list[MessageRecord] = []
    pending: set[str] = set()
    for message in messages:
        if frame and (
            message.tool_calls or message.run_id is not None and message.run_id != frame[0].run_id
        ):
            yield frame
            frame = []
            pending.clear()
        frame.append(message)
        pending.update(call.id for call in message.tool_calls)
        result = message.tool_result
        if result is not None and result.producer.kind == "model":
            pending.discard(result.tool_call_id)
        if not pending:
            yield frame
            frame = []
    if frame:
        yield frame


def _source_excerpts(
    store: SessionRepository, source: SessionRecord, after: int
) -> Iterator[HistoryExcerpt]:
    for frame in _source_frames(_source_messages(store, source.session_id, after)):
        visible, blocked = published_history(store, source, frame)
        lines = [text for message in visible if (text := _excerpt(message)) is not None]
        if lines:
            end = visible[-1]
            yield HistoryExcerpt(
                sequence=end.sequence, message_id=end.message_id, text="\n".join(lines)
            )
        if blocked:
            return


def _fit_excerpts(
    entries: Iterable[HistoryExcerpt],
    after: MessageBoundary | None,
    budget: int,
    estimator: TokenEstimator = estimate_text_tokens,
) -> list[HistoryExcerpt]:
    selected: list[HistoryExcerpt] = []
    remaining = budget
    start = after.sequence + 1 if after else 1
    for entry in entries:
        cost = estimator(entry.text) + estimator("\n")
        if cost > budget:
            notice = (
                f"[Source messages {start}–{entry.sequence} omitted: exceeds the input budget.]"
            )
            entry = entry.model_copy(update={"text": notice, "omitted": True})
            cost = estimator(notice) + estimator("\n")
        if cost > remaining:
            break
        selected.append(entry)
        remaining -= cost
        start = entry.sequence + 1
    return selected


def freeze_source_history(
    store: SessionRepository, target_id: str, *, input_budget: int
) -> SourceHistory | None:
    with store.session_read(target_id):
        target = store.load_session_record(target_id)
        source_id = target.context_source_session_id
        if source_id is None:
            return None
        epoch, after = _delivered_cursor(store, target, _delivery_key(target_id, source_id))
    with store.session_read(source_id):
        source = store.load_session_record(source_id)
        start = after.sequence if after else 0
        if (
            source.history_epoch < epoch
            or after
            and (
                start > source.message_count
                or store.message_id_at(source_id, start) != after.message_id
            )
        ):
            raise SessionBranchError(
                "Source history was rewritten. This conversation keeps its previous context."
            )
        if source.message_count == start:
            return None
        excerpts = _source_excerpts(store, source, start)
        first = next(excerpts, None)
        if first is None:
            return None
        entries = _fit_excerpts(chain((first,), excerpts), after, input_budget)
        if not entries:
            raise SessionBranchError("No source progress fits the selected model's input budget.")
        return SourceHistory(
            session_id=source_id,
            history_epoch=source.history_epoch,
            after=after,
            entries=tuple(entries),
        )


def _excerpt(message: MessageRecord) -> str | None:
    if message.metadata.get("context_provider_delivery_key") is not None:
        return None
    if message.tool_result is not None and message.tool_result.producer.kind != "model":
        return None
    if (
        message.role not in {"user", "assistant", "tool_result"}
        and message.semantic_tag != "attachments"
    ):
        return None
    lines = [message.text_content] if message.text_content else []
    for call in message.tool_calls:
        lines.append(f"{call.name}({json.dumps(call.arguments, ensure_ascii=False)})")
    label: str = message.role
    if result := message.tool_result:
        label = f"{result.tool_name} {'error' if result.is_error else 'result'}"
    lines.extend(
        f"[{block.type}]" for block in message.content if block.type in {"image", "audio", "video"}
    )
    lines.extend(f"/{block.name}" for block in message.content if block.type == "skill_activation")
    if message.semantic_tag == "attachments":
        lines.extend(item.name for item in message.input_attachments)
    body = "\n".join(lines)
    return f"[{label}] {body}" if body or result else None


def prepend_source_history(
    store: SessionRepository,
    target: SessionRecord,
    run_id: str,
    history: SourceHistory,
    preceding: Sequence[MessageRecord],
    unit: list[MessageRecord],
    budget: int,
    estimator: TokenEstimator,
) -> tuple[int, EventRecord | None]:
    """Prepend undelivered evidence to the input frame, or report a conflicting cursor."""
    key = _delivery_key(target.session_id, history.session_id)
    epoch, cursor = _delivered_cursor(store, target, key, preceding)
    if cursor != history.after:
        if (
            cursor
            and cursor.sequence > history.entries[-1].sequence
            and epoch == history.history_epoch
        ):
            return budget, None
        if not cursor or not any(
            entry.sequence == cursor.sequence and entry.message_id == cursor.message_id
            for entry in history.entries
        ):
            return budget, EventRecord(
                session_id=target.session_id,
                run_id=run_id,
                event_type="diagnostic",
                payload={
                    "code": "source_history_conflict",
                    "severity": "warning",
                    "message": "Source history changed before this input was consumed. "
                    "The question uses the conversation's previously committed context.",
                },
            )
    entries = [
        entry for entry in history.entries if cursor is None or entry.sequence > cursor.sequence
    ]
    if not entries:
        return budget, None
    note = MessageRecord.text(
        session_id=target.session_id,
        run_id=run_id,
        role="user",
        sequence=unit[0].sequence,
        semantic_tag="source-session-context",
        text="",
    )
    selected = _fit_excerpts(
        entries, cursor, budget - estimate_message_tokens(note, estimator), estimator
    )
    warning = None
    if not selected or any(entry.omitted for entry in selected):
        detail = " ".join(entry.text for entry in selected if entry.omitted)
        warning = EventRecord(
            session_id=target.session_id,
            run_id=run_id,
            event_type="diagnostic",
            payload={
                "code": "source_history_budget",
                "severity": "warning",
                "message": detail or "No source progress fits alongside this question.",
            },
        )
    if not selected:
        return budget, warning
    end = selected[-1]
    note.content = [TextContent(text="\n".join(entry.text for entry in selected))]
    group = unit[0].metadata.get("session_input_batch_id", unit[0].input_id)
    note.metadata = {
        "session_input_batch_id": group,
        "context_provider_delivery_key": key,
        "source_session_id": history.session_id,
        "source_history_epoch": history.history_epoch,
        "source_after": cursor.model_dump() if cursor else None,
        "source_boundary": {"sequence": end.sequence, "message_id": end.message_id},
    }
    for member in unit:
        member.metadata.update(
            input_context_message_id=note.message_id, session_input_batch_id=group
        )
    unit.insert(0, note)
    return budget - estimate_message_tokens(note, estimator), warning
