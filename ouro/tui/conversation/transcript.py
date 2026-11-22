"""Project session messages and activities into transcript entries."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from ouro.application.contracts.execution import RuntimeToolCallActivity
from ouro.core.attachment_input import is_direct_user_input, is_editable_user_input
from ouro.core.compaction import CompactionRecord
from ouro.core.message_statistics import delegation_input_id
from ouro.core.messages import MessageRecord, ModelMessage, SkillActivationContent
from ouro.tui.conversation.history_models import (
    CompactionPresentation,
    MessageSource,
    TranscriptEntry,
)
from ouro.tui.conversation.markdown_stream import split_markdown_blocks
from ouro.tui.input_presentation import reference_labels
from ouro.tui.state import RunPresentation


def _tool_call_keys(message: MessageRecord) -> dict[str, tuple[str | None, str | None, str]]:
    batches = {ref.tool_call_id: ref.batch_id for ref in message.call_refs}
    return {call.id: (message.run_id, batches.get(call.id), call.id) for call in message.tool_calls}


def _tool_activities_from_messages(
    messages: Iterable[MessageRecord],
) -> dict[tuple[str | None, str | None, str], RuntimeToolCallActivity]:
    """Pair assistant tool-call requests with their tool_result outcomes.

    A persisted tool call spans two messages: the assistant message carrying
    ``tool_calls`` and a following ``tool_result`` message carrying the
    outcome reference. This rebuilds the same ``RuntimeToolCallActivity``
    shape the live event stream produces, so ``ToolActivityRow`` renders
    history and live runs through one code path.

    ``MessageRecord`` does not store tool ``result_data``, so summaries that
    depend on results (read line counts) degrade to
    argument-only display.
    """
    activities: dict[tuple[str | None, str | None, str], RuntimeToolCallActivity] = {}
    for message in messages:
        if message.role == "assistant":
            keys = _tool_call_keys(message)
            for call in message.tool_calls:
                key = keys[call.id]
                activities[key] = RuntimeToolCallActivity(
                    batch_id=key[1],
                    tool_call_id=call.id,
                    tool_name=call.name,
                    status="unknown_outcome",
                    source_index=call.source_index,
                    arguments=call.arguments,
                    requested_at=message.created_at,
                )
        elif message.role == "tool_result" and message.tool_result is not None:
            ref = message.tool_result
            batch_id = message.outcome_ref.identity.batch_id if message.outcome_ref else None
            key = (message.run_id, batch_id, ref.tool_call_id)
            activity = activities.get(key)
            if activity is not None:
                activities[key] = activity.model_copy(
                    update={
                        "status": "failed" if ref.is_error else "completed",
                        "started_at": (
                            message.tool_execution.started_at
                            if message.tool_execution is not None
                            else None
                        ),
                        "ended_at": (
                            message.tool_execution.ended_at
                            if message.tool_execution is not None
                            else None
                        ),
                    }
                )
    return activities


def _compaction_presentation(
    *,
    compaction_id: str | None = None,
    covered_message_count: int,
    tokens_before: int | None,
    tokens_after: int | None,
    duration_seconds: float | None,
) -> CompactionPresentation:
    tokens_reduced: int | None = None
    if (
        tokens_before is not None
        and tokens_after is not None
        and (tokens_before > 0 or tokens_after > 0)
    ):
        tokens_reduced = max(0, tokens_before - tokens_after)
    return CompactionPresentation(
        compaction_id=compaction_id,
        covered_message_count=covered_message_count,
        tokens_reduced=tokens_reduced,
        duration_seconds=duration_seconds,
    )


def _agent_message_source(message: MessageRecord) -> MessageSource | None:
    if message.semantic_tag == "subagent-task" and delegation_input_id(message):
        return MessageSource("Main")
    if message.metadata.get("session_input_source") != "host":
        return None
    host = message.metadata.get("host_source_id")
    if not isinstance(host, str) or not host.startswith("ouro.subagents/"):
        return None
    if message.semantic_tag == "subagent-message":
        return MessageSource("Main")
    if message.semantic_tag == "subagent-result":
        attributes = message.semantic_attributes
        name = attributes.get("nickname") or attributes.get("agent_name")
        delegation_id = attributes.get("delegation_id")
        if name and delegation_id:
            return MessageSource(name, delegation_id)
    return None


def _input_transcript_entry(
    message: MessageRecord,
    *,
    show_harness_messages: bool,
) -> TranscriptEntry | None:
    """Project model inputs and Driver receipts into the visible transcript."""
    kind: Literal["user", "harness"]
    if message.semantic_tag == "source-session-context":
        return None
    if is_editable_user_input(message):
        kind = "user"
    elif show_harness_messages and (
        message.role == "harness"
        or (message.tool_result is not None and message.tool_result.producer.kind == "driver")
    ):
        kind = "harness"
    else:
        return None
    text, reference_start = _user_message_presentation(
        message, include_harness_content=show_harness_messages
    )
    if not text:
        return None
    return TranscriptEntry(
        kind=kind,
        text=text,
        created_at=message.created_at,
        run_id=message.run_id,
        reference_start=reference_start,
        message_source=_agent_message_source(message) if kind == "harness" else None,
        source_sequence=message.sequence,
        message_id=message.message_id,
        direct_user_input=is_direct_user_input(message),
    )


def _answer_entries(message: MessageRecord) -> Iterable[TranscriptEntry]:
    for block in split_markdown_blocks(message.text_content):
        yield TranscriptEntry(
            kind="assistant", text=block, run_id=message.run_id, source_sequence=message.sequence
        )


def project_dialogue_entries(messages: Iterable[MessageRecord]) -> tuple[TranscriptEntry, ...]:
    """Show only questions and public answers authored in the selected conversation."""
    entries: list[TranscriptEntry] = []
    for message in messages:
        if "source_message_id" in message.metadata:
            continue
        if message.role == "assistant":
            entries.extend(_answer_entries(message))
        elif is_direct_user_input(message):
            entry = _input_transcript_entry(message, show_harness_messages=False)
            if entry is not None:
                entries.append(entry)
    return tuple(entries)


def project_transcript_entries(
    messages: tuple[MessageRecord, ...],
    compactions: tuple[CompactionRecord, ...],
    runs: tuple[RunPresentation, ...],
    *,
    mutable_tool_call_keys: frozenset[tuple[str | None, str | None, str]],
    show_harness_messages: bool = False,
) -> tuple[TranscriptEntry, ...]:
    """Build immutable history away from Textual's event loop.

    Entries are bucketed directly at their message boundary. This preserves the
    canonical message → Run summary → compaction order without constructing and
    sorting an intermediate list of ordering tuples.
    """

    requests = {request.message_id: request for run in runs for request in run.requests}
    tool_activities = _tool_activities_from_messages(messages)
    message_entries: list[list[TranscriptEntry]] = [[] for _ in messages]
    message_positions: dict[str, int] = {}
    last_message_by_run: dict[str, int] = {}

    for message_index, message in enumerate(messages):
        message_positions[message.message_id] = message_index
        entries = message_entries[message_index]
        if message.role != "assistant":
            entry = _input_transcript_entry(message, show_harness_messages=show_harness_messages)
            if entry is not None:
                entries.append(entry)
            elif message.role == "harness":
                continue
        if message.run_id is not None:
            last_message_by_run[message.run_id] = message_index
        if message.role != "assistant":
            continue

        request = requests.get(message.message_id)
        if message.reasoning_content:
            entries.append(
                TranscriptEntry(
                    kind="assistant",
                    reasoning=message.reasoning_content,
                    run_id=message.run_id,
                    source_sequence=message.sequence,
                    reasoning_seconds=(request.reasoning_seconds if request is not None else None),
                )
            )
        entries.extend(_answer_entries(message))
        for key in _tool_call_keys(message).values():
            if key in mutable_tool_call_keys:
                continue
            activity = tool_activities.get(key)
            if activity is not None:
                entries.append(
                    TranscriptEntry(
                        kind="tool",
                        run_id=message.run_id,
                        tool=activity,
                        source_sequence=message.sequence,
                    )
                )

    for run in runs:
        boundary_index = last_message_by_run.get(run.run_id)
        if boundary_index is None or run.duration_seconds is None:
            continue
        message_entries[boundary_index].append(
            TranscriptEntry(
                kind="run",
                run_id=run.run_id,
                run=run,
                source_sequence=messages[boundary_index].sequence,
            )
        )

    for compaction in compactions:
        boundary_index = message_positions.get(compaction.history_boundary_message_id)
        if boundary_index is None:
            raise ValueError(
                f"compaction {compaction.compaction_id} references missing history "
                f"boundary {compaction.history_boundary_message_id}"
            )
        message_entries[boundary_index].append(
            TranscriptEntry(
                kind="compaction",
                source_sequence=messages[boundary_index].sequence,
                compaction=_compaction_presentation(
                    compaction_id=compaction.compaction_id,
                    covered_message_count=compaction.covered_message_count,
                    tokens_before=compaction.estimated_tokens_before,
                    tokens_after=compaction.estimated_tokens_after,
                    duration_seconds=compaction.duration_seconds,
                ),
            )
        )

    return tuple(entry for entries in message_entries for entry in entries)


def _user_message_presentation(
    message: MessageRecord | ModelMessage, *, include_harness_content: bool = False
) -> tuple[str, int | None]:
    """Keep copy text intact while identifying its semantic reference suffix."""
    attachments = message.input_attachments if isinstance(message, MessageRecord) else ()
    activations = tuple(
        block for block in message.content if isinstance(block, SkillActivationContent)
    )
    elements = sorted((*attachments, *activations), key=lambda item: (item.text_offset, item.order))
    text = message.text_content
    if attachments and message.semantic_tag == "attachments" and not include_harness_content:
        text = ""
    if not elements:
        return text, None
    prefix = f"{text}\n" if text.strip() else ""
    return prefix + reference_labels(item.name for item in elements), len(prefix)
