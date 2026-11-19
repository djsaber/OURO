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
    """Keep each model call and its results contiguous, in recorded result order."""

    pending: dict[tuple[str | None, str | None, str], int] = {}
    batches: dict[int, list[MessageRecord]] = {}
    for position, message in enumerate(messages):
        if message.tool_calls:
            batches[position] = []
            for call in message.tool_calls:
                pending[message.run_id, message.turn_id, call.id] = position
        elif (result := message.tool_result) is not None and result.producer.kind == "model":
            owner = pending.pop((message.run_id, message.turn_id, result.tool_call_id), None)
            if owner is not None:
                batches[owner].append(message)
    projected = []
    included_results: set[str] = set()
    excluded = []
    for position, message in enumerate(messages):
        if message.tool_calls:
            batch = batches[position]
            if message.metadata.get("incomplete") or len(batch) != len(message.tool_calls):
                excluded.append(message.message_id)
                continue
            projected.append(message)
            projected.extend(batch)
            included_results.update(result.message_id for result in batch)
        elif message.tool_result is not None and message.tool_result.producer.kind == "model":
            if message.message_id not in included_results:
                excluded.append(message.message_id)
        else:
            projected.append(message)
    return projected, tuple(excluded)


def conversation_turns(messages: Sequence[MessageRecord]) -> list[list[MessageRecord]]:
    """Group messages by user requests when no model-turn identity is available."""

    turns: list[list[MessageRecord]] = []
    for message in messages:
        if not turns or _starts_user_turn(message, turns[-1][-1]):
            turns.append([])
        turns[-1].append(message)
    return turns


def _starts_user_turn(message: MessageRecord, previous: MessageRecord) -> bool:
    if message.role != "user" and not (message.role == "harness" and message.input_ref is not None):
        return False
    batch_id = message.metadata.get("session_input_batch_id")
    return not batch_id or batch_id != previous.metadata.get("session_input_batch_id")


def context_frames(messages: Sequence[MessageRecord]) -> list[list[MessageRecord]]:
    """Group messages at durable model-turn boundaries.

    Messages in a model/tool exchange share a ``turn_id``. A preceding user message has no
    ``turn_id`` and joins the following identified frame. Messages outside a model/tool exchange
    are grouped by user request. If an incomplete tool exchange is encountered, the remaining
    suffix becomes one protected frame so projections cannot separate it from recovery context.
    """

    frames: list[list[MessageRecord]] = []
    previous: MessageRecord | None = None
    for message in messages:
        if (
            previous is None
            or (previous.turn_id is not None and previous.turn_id != message.turn_id)
            or (message.turn_id is None and _starts_user_turn(message, previous))
        ):
            frames.append([])
        frames[-1].append(message)
        previous = message
    for index, frame in enumerate(frames):
        if not _is_complete_frame(frame):
            for tail in frames[index + 1 :]:
                frame.extend(tail)
            del frames[index + 1 :]
            break
    return frames


def _is_complete_frame(frame: Sequence[MessageRecord]) -> bool:
    expected = {call.id for message in frame for call in message.tool_calls if call.id}
    if not expected:
        return True
    return expected.issubset(
        result.tool_call_id
        for message in frame
        if (result := message.tool_result) is not None and result.producer.kind == "model"
    )
