"""Present one Application observation page on the conversation timeline."""

from __future__ import annotations

from collections.abc import Iterator

from ouro.application.contracts.events import RuntimeEventView
from ouro.application.contracts.execution import RuntimeToolCallActivity
from ouro.application.contracts.models import AvailableModel
from ouro.application.contracts.sessions import SessionObservationFacts
from ouro.application.observations.content import SessionContentProjection
from ouro.application.observations.sessions import observation_event
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.view import ConversationView
from ouro.tui.state import project_run_presentation

EVENTS_PER_FRAME = 64


def event_frames(events: tuple[RuntimeEventView, ...]) -> Iterator[tuple[EventRecord, ...]]:
    """Expose a bounded prefix, keeping streamed tools visible before their next phase."""
    start = 0
    while start < len(events):
        end = min(start + EVENTS_PER_FRAME, len(events))
        for index in range(start + 1, end):
            if (
                events[index - 1].event_type == "model_tool_call_delta"
                and events[index].event_type != "model_tool_call_delta"
            ):
                end = index
                break
        yield tuple(observation_event(event) for event in events[start:end])
        start = end


async def present_committed_messages(
    conversation: ConversationView,
    projection: SessionContentProjection,
    facts: SessionObservationFacts,
    messages: tuple[MessageRecord, ...],
    *,
    through: int,
    history_run_ids: set[str],
    models: tuple[AvailableModel, ...],
) -> None:
    """Place messages, completed runs and compactions at their committed sequence boundaries."""
    history = conversation.query_one(ConversationHistory)
    if facts.session.active_run_id is not None:
        await history.reopen_run(facts.session.active_run_id)
    history_run_ids.update(message.run_id for message in messages if message.run_id)
    boundaries = sorted(
        [
            (end, run.ended_at, run)
            for run in facts.runs
            if run.kind == "agent"
            and run.ended_at is not None
            and (
                run.run_id in history_run_ids
                or (
                    run.run_id == facts.session.last_run_id
                    and run.history_epoch == facts.session.history_epoch
                )
            )
            and (end := facts.run_ends[run.run_id]) <= through
        ]
        + [
            (facts.compaction_ends[item.compaction_id], item.created_at, item)
            for item in facts.compactions
        ],
        key=lambda item: (item[0], item[1]),
    )
    boundary_index = 0

    async def finish_through(sequence: int) -> None:
        nonlocal boundary_index
        while boundary_index < len(boundaries) and boundaries[boundary_index][0] <= sequence:
            record = boundaries[boundary_index][2]
            boundary_index += 1
            if isinstance(record, RunRecord):
                await conversation.sync_run_summary(project_run_presentation(record, models=models))
            else:
                await conversation.add_compaction(
                    compaction_id=record.compaction_id,
                    covered_message_count=record.covered_message_count,
                    tokens_before=record.estimated_tokens_before,
                    tokens_after=record.estimated_tokens_after,
                    duration_seconds=record.duration_seconds,
                )

    for message in messages:
        await finish_through(message.sequence - 1)
        if projection.has_message(message.message_id, message.sequence):
            continue
        if message.run_id is not None:
            await history.reopen_run(message.run_id)
        kind = message.run_message_kind
        if kind == "assistant_response":
            if not projection.is_current_response(message):
                await conversation.finish_assistant()
            conversation.begin_model_request(run_id=message.run_id)
            answer, reasoning, duration = projection.apply_response(message)
            if reasoning:
                await conversation.append_reasoning(reasoning)
            if duration is not None:
                conversation.finish_reasoning(duration)
            if answer:
                await conversation.append_assistant(answer)
            if message.tool_calls:
                batches = {ref.tool_call_id: ref.batch_id for ref in message.call_refs}
                await conversation.sync_tools(
                    tuple(
                        RuntimeToolCallActivity(
                            run_id=message.run_id,
                            batch_id=batches.get(call.id),
                            tool_call_id=call.id,
                            tool_name=call.name,
                            status="planned",
                            source_index=call.source_index,
                            arguments=call.arguments,
                            requested_at=message.created_at,
                        )
                        for call in message.tool_calls
                    )
                )
        elif kind == "tool_result":
            conversation.begin_model_request(run_id=message.run_id)
            await conversation.apply_tool_result(message)
        else:
            await conversation.finish_assistant()
            await conversation.add_input_message(message)
        projection.remember_message(message.message_id, message.sequence)
    if projection.apply_facts(facts):
        await conversation.finish_assistant()
        conversation.begin_model_request(run_id=projection.activity.run_id)
    await conversation.sync_tools(projection.activity.tool_calls)
    await finish_through(through)
