from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable

from ouro.core.attachment_input import is_editable_user_input
from ouro.core.base import new_id
from ouro.core.compaction import CompactionRecord, validate_compaction_sources
from ouro.core.context import estimate_message_tokens
from ouro.core.context_selection import ContextSelection, MessageBoundary, validate_media_selection
from ouro.core.errors import CompactionStateError, SessionBranchError
from ouro.core.harness import HarnessState, HarnessVersion
from ouro.core.input_content import InputAttachment
from ouro.core.message_frames import project_tool_exchanges
from ouro.core.messages import MessageRecord
from ouro.core.session_controls import SessionControlState
from ouro.core.session_ports import SessionRepository
from ouro.core.state import SessionRecord


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
    return older.turn_id is None and older.role in {"user", "harness"} and newer.turn_id is not None


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
    batch_id = selected.metadata.get("session_input_batch_id")
    while (
        index > 0
        and batch_id is not None
        and messages[index - 1].metadata.get("session_input_batch_id") == batch_id
    ):
        index -= 1
    return messages[index - 1].message_id if index else None, input_resource_refs(selected)


def input_resource_refs(message: MessageRecord) -> tuple[str, ...]:
    uploads = (
        item.resource_id for item in message.input_attachments if isinstance(item, InputAttachment)
    )
    return tuple(dict.fromkeys((*uploads, *message.resource_refs)))


def published_history(
    store: SessionRepository, source: SessionRecord, messages: list[MessageRecord]
) -> tuple[list[MessageRecord], bool]:
    """Return published source records and whether a recoverable tool batch blocks the suffix."""
    _, excluded = project_tool_exchanges(messages)
    missing = set(excluded)
    visible: list[MessageRecord] = []
    for message in messages:
        if message.message_id in missing:
            if message.tool_calls and message.run_id:
                run = store.load_run(source.session_id, message.run_id)
                batch = run.active_tool_batch
                if (
                    run.history_epoch == source.history_epoch
                    and run.needs_recovery
                    and batch is not None
                    and batch.assistant_message_id == message.message_id
                ):
                    return visible, True
        elif not message.metadata.get("incomplete"):
            visible.append(message)
    return visible, False


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
        for reference in ("attachment_message_id", "input_context_message_id"):
            receipt = metadata.get(reference)
            if isinstance(receipt, str):
                if receipt not in message_id_map:
                    raise SessionBranchError("input is missing its receipt")
                metadata[reference] = message_id_map[receipt]
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


def copy_branch_context(
    session_id: str,
    source_messages: list[MessageRecord],
    copied_messages: list[MessageRecord],
    compactions: list[CompactionRecord],
    selection: ContextSelection,
    message_id_map: dict[str, str],
    *,
    source_end_sequence: int,
) -> tuple[ContextSelection, list[CompactionRecord]]:
    """Validate anchors in the source and project them onto the retained branch history."""
    if not copied_messages or (not selection.compaction_id and not selection.media_elided_through):
        return ContextSelection(), []
    sources = {message.message_id: message for message in source_messages}
    retained_sequences = [m.sequence for m in source_messages if m.message_id in message_id_map]
    source_id = source_messages[0].session_id

    def copied_through(sequence: int) -> MessageRecord | None:
        count = bisect_right(retained_sequences, sequence)
        return copied_messages[count - 1] if count else None

    media = selection.media_elided_through
    if media is not None:
        validate_media_selection(selection, source_id, sources.get(media.message_id))
        retained = copied_through(media.sequence)
        media = (
            MessageBoundary(message_id=retained.message_id, sequence=retained.sequence)
            if retained
            else None
        )
    copied: list[CompactionRecord] = []
    for compaction in _active_compaction_lineage(compactions, selection.compaction_id):
        if compaction.covered_message_count > source_end_sequence:
            break
        validate_compaction_sources(compaction, source_id, len(source_messages), sources)
        endpoint = copied_through(compaction.covered_message_count)
        if endpoint is None or endpoint.sequence != compaction.covered_message_count:
            break
        placement = sources[compaction.history_boundary_message_id]
        boundary = copied_through(placement.sequence)
        assert boundary is not None
        metadata = dict(compaction.metadata)
        metadata["source_compaction_id"] = compaction.compaction_id
        copied.append(
            compaction.model_copy(
                update={
                    "compaction_id": new_id("cmp"),
                    "session_id": session_id,
                    "previous_compaction_id": copied[-1].compaction_id if copied else None,
                    "covered_message_count": endpoint.sequence,
                    "covered_end_message_id": endpoint.message_id,
                    "retained_skill_message_ids": tuple(
                        message_id_map[identity]
                        for identity in compaction.retained_skill_message_ids
                        if identity in message_id_map
                    ),
                    "history_boundary_message_id": boundary.message_id,
                    "context_metrics_after": compaction.context_metrics_after
                    if boundary.sequence == placement.sequence
                    else None,
                    "metadata": metadata,
                }
            )
        )
    if copied:
        copied[-1] = _extend_branch_context_metrics(copied[-1], copied_messages)
    return ContextSelection(
        compaction_id=copied[-1].compaction_id if copied else None,
        media_elided_through=media,
    ), copied


def _active_compaction_lineage(
    compactions: list[CompactionRecord],
    active_compaction_id: str | None,
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
