from __future__ import annotations

from ouro.application.session_models import ReplayFrame
from ouro.core.base import new_id
from ouro.core.compaction import (
    CompactionRecord,
    compaction_source_digest,
    validate_compaction_lineage,
)
from ouro.core.context import estimate_message_tokens
from ouro.core.errors import CompactionStateError, SessionBranchError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.session_extensions import SessionExtensionStateRecord


def select_branch_messages(
    messages: list[MessageRecord],
    requested_message_id: str | None,
) -> tuple[list[MessageRecord], MessageRecord | None]:
    if requested_message_id is None:
        return [], None
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


def copy_branch_compactions(
    session_id: str,
    source_messages: list[MessageRecord],
    copied_messages: list[MessageRecord],
    compactions: list[CompactionRecord],
    active_compaction_id: str | None,
    message_id_map: dict[str, str],
) -> list[CompactionRecord]:
    """Copy the usable active compaction lineage into a branch.

    A compaction is usable only when both its summarized prefix and the durable
    message boundary where it became history are present in the branch. This
    prevents a branch from inheriting a summary that reaches beyond its own
    canonical messages, and keeps transcript placement references resolvable.
    """

    if active_compaction_id is None:
        return []
    if len(source_messages) != len(copied_messages):
        raise ValueError("source and copied branch messages must have equal lengths")

    lineage = _active_compaction_lineage(compactions, active_compaction_id)
    source_ids = [message.message_id for message in source_messages]
    source_positions = {message_id: index for index, message_id in enumerate(source_ids)}
    compaction_id_map: dict[str, str] = {}
    copied: list[CompactionRecord] = []

    for compaction in lineage:
        covered_count = len(compaction.covered_message_ids)
        if covered_count == 0:
            raise CompactionStateError(
                f"compaction {compaction.compaction_id} covers no canonical messages"
            )
        if source_ids[:covered_count] != compaction.covered_message_ids:
            break

        boundary_index = source_positions.get(compaction.history_boundary_message_id)
        if boundary_index is None:
            break
        if boundary_index < covered_count - 1:
            raise CompactionStateError(
                f"compaction {compaction.compaction_id} history boundary precedes its summary"
            )

        validate_compaction_lineage(source_messages, compaction)
        previous_id = compaction.previous_compaction_id
        if previous_id is not None and previous_id not in compaction_id_map:
            break

        copied_id = new_id("cmp")
        compaction_id_map[compaction.compaction_id] = copied_id
        copied_prefix = copied_messages[:covered_count]
        metadata = dict(compaction.metadata)
        metadata["source_compaction_id"] = compaction.compaction_id
        copied.append(
            compaction.model_copy(
                update={
                    "compaction_id": copied_id,
                    "session_id": session_id,
                    "previous_compaction_id": (
                        compaction_id_map[previous_id] if previous_id is not None else None
                    ),
                    "covered_message_ids": [
                        message_id_map[message_id]
                        for message_id in compaction.covered_message_ids
                    ],
                    "history_boundary_message_id": message_id_map[
                        compaction.history_boundary_message_id
                    ],
                    "source_digest": compaction_source_digest(copied_prefix),
                    "metadata": metadata,
                }
            )
        )
    if copied:
        copied[-1] = _extend_branch_context_metrics(copied[-1], copied_messages)
    return copied


def _active_compaction_lineage(
    compactions: list[CompactionRecord],
    active_compaction_id: str,
) -> list[CompactionRecord]:
    records = {compaction.compaction_id: compaction for compaction in compactions}
    reverse_lineage: list[CompactionRecord] = []
    visited: set[str] = set()
    current_id: str | None = active_compaction_id
    while current_id is not None:
        if current_id in visited:
            raise CompactionStateError(f"compaction lineage contains a cycle at {current_id}")
        visited.add(current_id)
        current = records.get(current_id)
        if current is None:
            raise CompactionStateError(
                f"active compaction lineage references missing compaction {current_id}"
            )
        reverse_lineage.append(current)
        current_id = current.previous_compaction_id
    return list(reversed(reverse_lineage))


def _extend_branch_context_metrics(
    compaction: CompactionRecord,
    messages: list[MessageRecord],
) -> CompactionRecord:
    """Advance inherited post-compaction metrics through the copied branch tail."""

    metrics = compaction.context_metrics_after
    if metrics is None:
        return compaction
    boundary_index = next(
        index
        for index, message in enumerate(messages)
        if message.message_id == compaction.history_boundary_message_id
    )
    tail = messages[boundary_index + 1 :]
    if not tail:
        return compaction

    estimated_input_tokens = metrics.estimated_input_tokens + sum(
        estimate_message_tokens(message) for message in tail
    )
    budget = metrics.input_budget_tokens
    effective_metrics = metrics.model_copy(
        update={
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_remaining_tokens": (
                budget - estimated_input_tokens if budget is not None else None
            ),
            "estimated_utilization_ratio": (
                estimated_input_tokens / budget if budget is not None else None
            ),
            "over_budget": budget is not None and estimated_input_tokens > budget,
        }
    )
    metadata = dict(compaction.metadata)
    metadata["branch_context_extended_through_message_id"] = messages[-1].message_id
    return compaction.model_copy(
        update={
            "estimated_tokens_after": estimated_input_tokens,
            "context_metrics_after": effective_metrics,
            "metadata": metadata,
        }
    )


def apply_extension_branch_policies(
    messages: list[MessageRecord],
    requested_message_id: str | None,
    states: list[SessionExtensionStateRecord],
    branch_session_id: str,
) -> tuple[list[MessageRecord], list[SessionExtensionStateRecord]]:
    """Apply owner-declared Session lifecycle rules before publishing a branch."""

    rejecting_owners = sorted(state.owner_id for state in states if state.branch_policy == "reject")
    if rejecting_owners:
        raise SessionBranchError(
            "branch rejected by Session extension state owners: " + ", ".join(rejecting_owners)
        )

    reset_tags = {
        tag
        for state in states
        if state.branch_policy == "reset"
        for tag in state.projection_semantic_tags
    }
    requested = next(
        (message for message in messages if message.message_id == requested_message_id),
        None,
    )
    if requested is not None and requested.semantic_tag in reset_tags:
        raise SessionBranchError(
            "branch point is an extension projection that must reset; "
            "choose an earlier durable message"
        )

    retained_messages = [message for message in messages if message.semantic_tag not in reset_tags]
    copied_states = [
        state.model_copy(update={"session_id": branch_session_id})
        for state in states
        if state.branch_policy == "copy"
    ]
    return retained_messages, copied_states


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
