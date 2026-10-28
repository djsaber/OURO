from __future__ import annotations

from collections.abc import Iterable

from ouro.application.contracts.sessions import ReplayFrame
from ouro.core.attachment_input import is_editable_user_input
from ouro.core.base import new_id
from ouro.core.compaction import (
    CompactionRecord,
)
from ouro.core.context import estimate_message_tokens
from ouro.core.errors import CompactionStateError, SessionBranchError
from ouro.core.events import EventRecord
from ouro.core.harness import HarnessState, HarnessVersion
from ouro.core.input_content import InputAttachment
from ouro.core.messages import MessageRecord
from ouro.core.session_controls import SessionControlState


def select_history_suffix(
    records: Iterable[tuple[int, MessageRecord]], *, limit: int
) -> tuple[tuple[MessageRecord, ...], int | None]:
    """Take a newest-first suffix without splitting a turn, input, or tool frame.

    The limit is soft: one complete frame may exceed it. The exclusive cursor
    belongs to the oldest retained message, independent of storage ordering keys.
    """
    messages: list[MessageRecord] = []
    pending_results: set[tuple[str | None, str | None, str]] = set()
    oldest_cursor: int | None = None
    for cursor, message in records:
        if (
            len(messages) >= limit
            and not pending_results
            and not _shares_history_frame(message, messages[-1])
        ):
            return tuple(reversed(messages)), oldest_cursor
        messages.append(message)
        oldest_cursor = cursor
        result = message.tool_result
        if result is not None and result.producer.kind == "model":
            pending_results.add((message.run_id, message.turn_id, result.tool_call_id))
        pending_results.difference_update(
            (message.run_id, message.turn_id, call.id) for call in message.tool_calls
        )
    return tuple(reversed(messages)), None


def _shares_history_frame(older: MessageRecord, newer: MessageRecord) -> bool:
    if older.run_id == newer.run_id and older.turn_id is not None:
        if older.turn_id == newer.turn_id:
            return True
    batch = older.metadata.get("session_input_batch_id")
    if batch is not None and batch == newer.metadata.get("session_input_batch_id"):
        return True
    return older.role == "user" and newer.turn_id is not None


def branch_input_boundary(
    messages: list[MessageRecord], message_id: str | None
) -> tuple[str | None, tuple[str, ...]]:
    """Exclude the complete editable input while retaining its draft's uploaded resources."""
    index = next(
        (index for index, message in enumerate(messages) if message.message_id == message_id),
        None,
    )
    if index is None or not is_editable_user_input(messages[index]):
        raise SessionBranchError(f"user message is not a fork point: {message_id}")
    selected = messages[index]
    resource_refs = tuple(
        dict.fromkeys(
            [
                item.resource_id
                for item in selected.input_attachments
                if isinstance(item, InputAttachment)
            ]
            + selected.resource_refs
        )
    )
    batch_id = selected.metadata.get("session_input_batch_id")
    while (
        index > 0
        and batch_id is not None
        and messages[index - 1].metadata.get("session_input_batch_id") == batch_id
    ):
        index -= 1
    return messages[index - 1].message_id if index else None, resource_refs


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
    batch_id = source_message.metadata.get("session_input_batch_id")
    if batch_id is not None:
        while (
            branch_end + 1 < len(messages)
            and messages[branch_end + 1].metadata.get("session_input_batch_id") == batch_id
        ):
            branch_end += 1
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
        attachment_id = metadata.get("attachment_message_id")
        if isinstance(attachment_id, str):
            if attachment_id not in message_id_map:
                raise SessionBranchError("attachment input is missing its receipt")
            metadata["attachment_message_id"] = message_id_map[attachment_id]
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

    Coverage and predecessor summaries are dependencies. A display anchor beyond
    the copied tail is moved to the branch head without discarding valid coverage.
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
        covered_count = compaction.covered_message_count
        if covered_count > len(source_ids):
            break
        if source_ids[covered_count - 1] != compaction.covered_end_message_id:
            raise CompactionStateError("compaction coverage boundary does not match branch history")

        boundary_index = source_positions.get(compaction.history_boundary_message_id)
        if boundary_index is not None and boundary_index < covered_count - 1:
            raise CompactionStateError(
                f"compaction {compaction.compaction_id} history boundary precedes its summary"
            )

        previous_id = compaction.previous_compaction_id
        if previous_id is not None and previous_id not in compaction_id_map:
            break

        copied_id = new_id("cmp")
        compaction_id_map[compaction.compaction_id] = copied_id
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
                    "covered_end_message_id": message_id_map[compaction.covered_end_message_id],
                    "retained_skill_message_ids": tuple(
                        message_id_map[identity]
                        for identity in compaction.retained_skill_message_ids
                    ),
                    "history_boundary_message_id": (
                        message_id_map[compaction.history_boundary_message_id]
                        if boundary_index is not None
                        else copied_messages[-1].message_id
                    ),
                    "context_metrics_after": compaction.context_metrics_after
                    if boundary_index is not None
                    else None,
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
    versions: list[HarnessVersion],
    branch_session_id: str,
) -> tuple[list[MessageRecord], list[HarnessVersion], dict[str, str]]:
    """Apply each owner's lifecycle policy across the complete retained history."""

    states = [state for version in versions for state in version.state.states]
    rejecting = sorted({state.owner_id for state in states if state.branch_policy == "reject"})
    if rejecting:
        raise SessionBranchError(
            "branch rejected by Session extension state owners: " + ", ".join(rejecting)
        )
    resetting = {state.owner_id for state in states if state.branch_policy == "reset"}
    copying = {state.owner_id for state in states if state.branch_policy == "copy"} - resetting
    reset_tags = {
        tag
        for state in states
        if state.owner_id in resetting
        for tag in state.projection_semantic_tags
    }
    requested = next(
        (message for message in messages if message.message_id == requested_message_id), None
    )
    if requested is not None and requested.semantic_tag in reset_tags:
        raise SessionBranchError(
            "branch point is an extension projection that must reset; "
            "choose an earlier durable message"
        )
    retained = [message for message in messages if message.semantic_tag not in reset_tags]
    identities = {version.version_id: new_id("harness") for version in versions}
    copied = [
        HarnessVersion(
            session_id=branch_session_id,
            version_id=identities[version.version_id],
            state=HarnessState(
                states=tuple(
                    state.model_copy(update={"session_id": branch_session_id})
                    for state in version.state.states
                    if state.owner_id in copying
                ),
                controls=SessionControlState(
                    activations=[
                        # Activation identity is Session-scoped and may be referenced
                        # by opaque owner data; copy it together with that state.
                        activation.model_copy(deep=True)
                        for activation in version.state.controls.activations
                        if activation.plugin_id in copying
                    ]
                ),
            ),
        )
        for version in versions
    ]
    return retained, copied, identities


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
