from __future__ import annotations

from collections.abc import Sequence

from ouro.core.base import OuroModel
from ouro.core.messages import MessageFact, MessageRecord


class MessageTruncation(OuroModel):
    """A canonical suffix removed immediately before one selected message."""

    first_removed: MessageFact
    last_retained: MessageFact | None
    removed_count: int


def project_tool_exchanges(
    messages: Sequence[MessageRecord],
) -> tuple[list[MessageRecord], tuple[str, ...]]:
    """Omit incomplete exchanges from Provider context without editing history."""

    results = {
        (message.run_id, message.turn_id, message.tool_result.tool_call_id)
        for message in messages
        if message.tool_result is not None and message.tool_result.producer.kind == "model"
    }
    complete_calls = set()
    complete_messages = set()
    for message in messages:
        if not message.tool_calls or message.metadata.get("incomplete"):
            continue
        calls = {(message.run_id, message.turn_id, call.id) for call in message.tool_calls}
        if calls <= results:
            complete_calls.update(calls)
            complete_messages.add(message.message_id)

    projected = []
    excluded = []
    for message in messages:
        incomplete = bool(message.tool_calls) and message.message_id not in complete_messages
        if message.tool_result is not None and message.tool_result.producer.kind == "model":
            incomplete = (
                message.run_id,
                message.turn_id,
                message.tool_result.tool_call_id,
            ) not in complete_calls
        if incomplete:
            excluded.append(message.message_id)
        else:
            projected.append(message)
    return projected, tuple(excluded)


def conversation_turns(messages: Sequence[MessageRecord]) -> list[list[MessageRecord]]:
    """Group messages by user requests when no model-turn identity is available."""

    turns: list[list[MessageRecord]] = []
    current: list[MessageRecord] = []
    for message in messages:
        same_input = bool(
            current
            and message.metadata.get("session_input_batch_id")
            and message.metadata.get("session_input_batch_id")
            == current[-1].metadata.get("session_input_batch_id")
        )
        if message.role == "user" and current and not same_input:
            turns.append(current)
            current = []
        current.append(message)
    if current:
        turns.append(current)
    return turns


def context_frames(messages: Sequence[MessageRecord]) -> list[list[MessageRecord]]:
    """Group messages at durable model-turn boundaries.

    Messages in a model/tool exchange share a ``turn_id``. A preceding user message has no
    ``turn_id`` and joins the following identified frame. Messages outside a model/tool exchange
    are grouped by user request. If an incomplete tool exchange is encountered, the remaining
    suffix becomes one protected frame so projections cannot separate it from recovery context.
    """

    frames: list[list[MessageRecord]] = []
    current: list[MessageRecord] = []
    current_turn_id: str | None = None
    pending_messages: list[MessageRecord] = []

    def flush_current() -> None:
        nonlocal current, current_turn_id
        if current:
            frames.append(current)
        current = []
        current_turn_id = None

    for message in messages:
        if message.turn_id is not None:
            if current and current_turn_id != message.turn_id:
                flush_current()
            if not current:
                pending_frames = conversation_turns(pending_messages)
                frames.extend(pending_frames[:-1])
                current = list(pending_frames[-1]) if pending_frames else []
                pending_messages = []
                current_turn_id = message.turn_id
            current.append(message)
            continue

        if current_turn_id is not None:
            flush_current()
        pending_messages.append(message)

    flush_current()
    frames.extend(conversation_turns(pending_messages))
    for index, frame in enumerate(frames):
        if _is_complete_frame(frame):
            continue
        return [*frames[:index], [message for tail in frames[index:] for message in tail]]
    return frames


def _tool_call_ids(message: MessageRecord) -> set[str]:
    if message.role != "assistant":
        return set()
    return {call.id for call in message.tool_calls if call.id}


def _tool_result_id(message: MessageRecord) -> str | None:
    return (
        message.tool_result.tool_call_id
        if message.tool_result is not None and message.tool_result.producer.kind == "model"
        else None
    )


def _is_complete_frame(frame: Sequence[MessageRecord]) -> bool:
    expected = {call_id for message in frame for call_id in _tool_call_ids(message)}
    if not expected:
        return True
    seen = {result_id for message in frame if (result_id := _tool_result_id(message)) is not None}
    return expected.issubset(seen)
