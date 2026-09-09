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


def validate_frame_boundary(
    first_removed: MessageRecord, last_retained: MessageRecord | None
) -> None:
    """Validate the cut itself, without imposing new validity rules on its prefix.

    Inputs start independent frames, even after interrupted output. Truncation
    may expose an already incomplete frame, but must not split an existing one.
    Protocol recovery before execution is a separate operation.
    """
    if first_removed.role in {"user", "system"}:
        return
    if first_removed.role == "tool_result" or (
        last_retained is not None
        and last_retained.role not in {"user", "system"}
        and first_removed.turn_id is not None
        and first_removed.turn_id == last_retained.turn_id
        and first_removed.run_id == last_retained.run_id
    ):
        raise ValueError("cannot truncate inside a message frame")


def incomplete_suffix_start(messages: Sequence[MessageRecord]) -> MessageRecord | None:
    """Find the first unfinished frame, preserving every complete input and exchange.

    Recovery must first commit known tool outcomes. An unfinished exchange is
    indivisible: exposing an assistant call without its results is invalid.
    """

    pending: set[str] = set()
    start: MessageRecord | None = None
    for message in messages:
        if message.metadata.get("incomplete"):
            return start or message
        if message.tool_calls:
            if start is None:
                start = message
            pending.update(call.id for call in message.tool_calls)
        if message.tool_result is not None:
            pending.discard(message.tool_result.tool_call_id)
            if not pending:
                start = None
    return start
