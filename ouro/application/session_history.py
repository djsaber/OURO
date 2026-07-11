from __future__ import annotations

from ouro.application.session_models import ReplayFrame
from ouro.core.base import new_id
from ouro.core.errors import SessionBranchError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord


def select_branch_messages(
    messages: list[MessageRecord],
    requested_message_id: str,
) -> tuple[list[MessageRecord], MessageRecord]:
    requested_index = next(
        (
            index
            for index, message in enumerate(messages)
            if message.message_id == requested_message_id
        ),
        None,
    )
    if requested_index is None:
        raise SessionBranchError(f"message not found in source session: {requested_message_id}")

    source_message = messages[requested_index]
    branch_end = requested_index
    for start, end, complete in _tool_exchange_ranges(messages):
        if not start <= requested_index <= end:
            continue
        if not complete:
            previous = messages[start - 1].message_id if start > 0 else "none"
            raise SessionBranchError(
                "branch point is inside an incomplete tool exchange; "
                f"nearest previous legal message id: {previous}"
            )
        branch_end = end
        break
    return messages[: branch_end + 1], source_message


def copy_branch_messages(
    session_id: str,
    messages: list[MessageRecord],
) -> tuple[list[MessageRecord], dict[str, str]]:
    copied: list[MessageRecord] = []
    message_id_map: dict[str, str] = {}
    for sequence, message in enumerate(messages, start=1):
        message_id = new_id("msg")
        message_id_map[message.message_id] = message_id
        metadata = dict(message.metadata)
        metadata["source_message_id"] = message.message_id
        if message.run_id is not None:
            metadata["source_run_id"] = message.run_id
        if message.turn_id is not None:
            metadata["source_turn_id"] = message.turn_id
        copied.append(
            message.model_copy(
                update={
                    "message_id": message_id,
                    "session_id": session_id,
                    "run_id": None,
                    "turn_id": None,
                    "sequence": sequence,
                    "metadata": metadata,
                }
            )
        )
    return copied, message_id_map


def project_replay_frames(events: list[EventRecord]) -> tuple[ReplayFrame, ...]:
    ordered = sorted(
        events,
        key=lambda event: (event.sequence, event.created_at, event.event_id),
    )
    return tuple(ReplayFrame(sequence=event.sequence, event=event) for event in ordered)


def _tool_exchange_ranges(messages: list[MessageRecord]) -> list[tuple[int, int, bool]]:
    ranges: list[tuple[int, int, bool]] = []
    for index, message in enumerate(messages):
        expected = _tool_call_ids(message)
        if not expected:
            continue
        seen: set[str] = set()
        end = index
        cursor = index + 1
        while cursor < len(messages) and messages[cursor].role == "tool_result":
            result_ref = messages[cursor].tool_result
            if result_ref is not None:
                seen.add(result_ref.tool_call_id)
            end = cursor
            cursor += 1
            if expected.issubset(seen):
                break
        ranges.append((index, end, expected.issubset(seen)))
    return ranges


def _tool_call_ids(message: MessageRecord) -> set[str]:
    if message.role != "assistant":
        return set()
    return {call.id for call in message.tool_calls if call.id}
