from __future__ import annotations

from collections.abc import Sequence

from ouro.core.base import OuroModel
from ouro.core.compaction import CompactionRecord
from ouro.core.messages import MessageRecord


class MessageTruncation(OuroModel):
    """A canonical suffix removed immediately before one selected message."""

    first_removed: MessageRecord
    last_retained: MessageRecord | None
    removed_count: int


def retain_compactions(
    compactions: Sequence[CompactionRecord], removed_message_ids: set[str]
) -> list[CompactionRecord]:
    """Remove summaries whose coverage, placement or previous summary was withdrawn."""
    retained: list[CompactionRecord] = []
    removed: set[str] = set()
    for compaction in compactions:
        if (
            compaction.history_boundary_message_id in removed_message_ids
            or removed_message_ids.intersection(compaction.covered_message_ids)
            or compaction.previous_compaction_id in removed
        ):
            removed.add(compaction.compaction_id)
        else:
            retained.append(compaction)
    return retained


def project_tool_exchanges(
    messages: Sequence[MessageRecord],
) -> tuple[list[MessageRecord], tuple[str, ...]]:
    """Omit incomplete exchanges from Provider context without editing history."""

    results = {
        (message.run_id, message.turn_id, message.tool_result.tool_call_id)
        for message in messages
        if message.tool_result is not None
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
        if message.tool_result is not None:
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
