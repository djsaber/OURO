"""One suffix reader for local catch-up, reattachment and other execution owners."""

from __future__ import annotations

from collections.abc import Callable

from ouro.application.contracts.events import project_runtime_event
from ouro.application.contracts.execution import RuntimeToolCallActivity
from ouro.application.contracts.sessions import (
    SessionContextStatus,
    SessionObservationBatch,
    SessionObservationFacts,
    SessionView,
)
from ouro.application.contracts.tool_activity import PendingInteraction
from ouro.application.inputs.queue import project_pending_user_inputs
from ouro.application.observations.activity import (
    project_batch_activity,
    project_batch_interactions,
)
from ouro.application.observations.drafts import read_draft_events
from ouro.application.runs.tool_batches import load_batch_call
from ouro.core.context_snapshots import ModelRequestHeader
from ouro.core.messages import MessageRecord
from ouro.core.session_observation import FactCursor, SessionObservationPosition
from ouro.core.tool_batches import ToolCallBatchRecord
from ouro.sessions.base import SessionStore


def read_observation_page(
    store: SessionStore,
    session_id: str,
    *,
    after: SessionObservationPosition,
    history_epoch: int,
    limit: int,
    context_status: Callable[[ModelRequestHeader], SessionContextStatus],
) -> tuple[SessionObservationBatch, bool]:
    with store.session_read(session_id):
        revision = store.observation_revision(session_id)
        facts = None
        messages: tuple[MessageRecord, ...] = ()
        more = False
        cursor = None
        message_after = after.message_sequence
        draft_after = after.draft
        fact_cursor = after.facts
        context = store.load_latest_context_header(session_id)
        if revision != after.revision or after.facts is not None:
            session = store.load_session_record(session_id)
            if session.history_epoch != history_epoch:
                # Consumers rebuild the invalidated view from a snapshot. The shared
                # follower resumes at that history's head without rereading its body.
                message_after = session.message_count
                draft_after = None
                fact_cursor = None
            history_epoch = session.history_epoch
            changed = store.load_observation_changes(
                session_id,
                after_revision=after.revision,
                through_revision=revision,
                cursor=fact_cursor,
                limit=limit,
            )
            records, more = store.load_messages_after(
                session_id,
                after_sequence=message_after,
                limit=limit,
            )
            messages = tuple(records)
            cursor = changed.next_cursor
            more = more or cursor is not None
            if more and cursor is None:
                cursor = FactCursor(revision=revision, kind=4, identity="")
            runs = {run.run_id: run for run in changed.runs}
            for run_id in dict.fromkeys(
                (
                    session.active_run_id or session.last_run_id,
                    *(message.run_id for message in messages),
                    *(state.identity.run_id for state in changed.calls),
                )
            ):
                if run_id is not None and run_id not in runs:
                    runs[run_id] = store.load_run(session_id, run_id)
            tools: list[RuntimeToolCallActivity] = []
            interactions: list[PendingInteraction] = []
            for state in changed.calls:
                run = runs[state.identity.run_id]
                head = run.active_tool_batch
                # Historical Calls remain in message history; only the selected batch is live.
                if (
                    run.history_epoch != history_epoch
                    or head is None
                    or head.batch_id != state.identity.batch_id
                ):
                    continue
                call = load_batch_call(store, session_id, state.identity, state=state)
                batch = ToolCallBatchRecord(**head.model_dump(exclude={"call_ids"}), calls=[call])
                tools.extend(
                    tool.model_copy(update={"run_id": run.run_id})
                    for tool in project_batch_activity(batch)
                )
                interactions.extend(project_batch_interactions(run, batch))
            inputs = [
                record.item
                for state in changed.inputs
                if state.status == "pending"
                and (record := store.load_input(session_id, state.input_id)) is not None
            ]
            current = context
            recent_id = session.active_run_id or session.last_run_id
            recent = runs.get(recent_id) if recent_id is not None else None
            if (
                recent is not None
                and recent.current_request_id is not None
                and (context is None or context.context_id != recent.current_request_id)
            ):
                current = store.load_request_header(session_id, recent.current_request_id)
            active_id = session.context_selection.compaction_id
            active = store.load_compaction(session_id, active_id) if active_id is not None else None
            metrics = context.metrics if context is not None else None
            if (
                active is not None
                and active.context_metrics_after is not None
                and (context is None or active.created_at > context.prepared_at)
            ):
                metrics = active.context_metrics_after
            message_end = messages[-1].sequence if messages else message_after
            compactions = {
                item.compaction_id: item
                for item in (
                    store.load_compactions_in_range(
                        session_id,
                        first_sequence=messages[0].sequence,
                        last_sequence=messages[-1].sequence,
                    )
                    if messages
                    else ()
                )
            }
            compactions.update((item.compaction_id, item) for item in changed.compactions)
            compaction_ends = {
                identity: store.load_message_fact(
                    session_id, item.history_boundary_message_id
                ).sequence
                for identity, item in compactions.items()
            }
            facts = SessionObservationFacts(
                session=SessionView.from_record(session),
                runs=tuple(runs.values()),
                run_ends={
                    run.run_id: store.run_message_end(session_id, run.run_id)
                    for run in runs.values()
                    if run.ended_at is not None
                },
                changed_calls=tuple(state.identity for state in changed.calls),
                tool_calls=tuple(tools),
                pending_interactions=tuple(interactions),
                inputs=changed.inputs,
                queued_inputs=project_pending_user_inputs(session, inputs),
                compactions=tuple(
                    item
                    for identity, item in compactions.items()
                    if compaction_ends[identity] <= message_end
                ),
                compaction_ends=compaction_ends,
                current_request=current,
                context_status=context_status(context) if context is not None else None,
                effective_context_metrics=metrics,
            )
        draft = (
            ()
            if more
            else read_draft_events(
                store,
                session_id,
                sequence=0,
                header=context,
                history_epoch=history_epoch,
                after=draft_after,
                limit=limit,
            )
        )
        position = SessionObservationPosition(
            revision=after.revision if more else revision,
            message_sequence=messages[-1].sequence if messages else message_after,
            facts=cursor,
            draft=draft[-1].stream if draft else draft_after,
        )
    return SessionObservationBatch(
        session_id=session_id,
        history_epoch=history_epoch,
        after=after,
        position=position,
        facts=facts,
        messages=messages,
        events=tuple(project_runtime_event(event) for event in draft),
    ), more or len(draft) == limit
