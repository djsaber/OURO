from __future__ import annotations

import asyncio

import pytest

from ouro.application.contracts.sessions import SessionHistoryAnchor
from ouro.application.sessions.service import SessionService
from ouro.core.compaction import CompactionRecord
from ouro.core.context import ContextBuilder, ContextMetrics
from ouro.core.context_snapshots import ContextSnapshotRecord
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord, ToolResultReference
from ouro.core.session_commits import SessionCommit
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolCallRequest
from tests.application.test_session_event_following import stores as stores
from tests.application.test_session_event_following import supervisor_for
from tests.context_facts import save_context
from tests.response_drafts import persist_response_draft
from tests.tool_batches import seed_tool_messages


def seed(store, count):
    session = SessionRecord()
    store.create_session(session)
    messages = [
        MessageRecord.text(session_id=session.session_id, role="user", text=f"message {index}")
        for index in range(count)
    ]
    store.append_messages(messages)
    return session.session_id, messages


def test_observation_status_does_not_materialize_provider_context(stores, monkeypatch):
    writer, reader = stores
    session_id, messages = seed(writer, 5)
    context = ContextSnapshotRecord(
        session_id=session_id,
        run_id="run",
        turn_id="turn",
        model_round=1,
        provider_id="test",
        model_id="test",
        provider_context=ContextBuilder().build(messages=messages, tools=[]).provider_context,
        metrics=ContextMetrics(estimated_input_tokens=50, estimated_fixed_tokens=0),
    )
    save_context(writer, context)

    def forbidden(*args, **kwargs):
        pytest.fail("status query materialized the lossless provider context")

    monkeypatch.setattr(reader, "load_latest_context", forbidden)
    service = SessionService(reader)
    snapshot = service.observation_snapshot(session_id, history_limit=2)
    assert snapshot.context_status.context_id == context.context_id
    assert snapshot.effective_context_metrics == context.metrics
    assert service.context_status(session_id) == snapshot.context_status
    assert service.effective_context_metrics(session_id) == context.metrics


def test_recent_window_reads_only_suffix_and_pages_without_overlap(stores, monkeypatch):
    writer, reader = stores
    session_id, messages = seed(writer, 1003)
    service = SessionService(reader)
    reads = []
    original = reader.iter_messages_before

    def measured(*args, **kwargs):
        iterator = original(*args, **kwargs)
        try:
            for cursor, message in iterator:
                reads.append(message.message_id)
                yield cursor, message
        finally:
            iterator.close()

    monkeypatch.setattr(reader, "iter_messages_before", measured)
    window = service.history_window(session_id, limit=20)
    assert window.messages == tuple(messages[-20:])
    assert len(reads) == 21
    collected = window.messages
    while window.before_cursor is not None:
        window = service.history_window(
            session_id, limit=20, before=window.before_cursor, history_epoch=window.history_epoch
        )
        collected = (*window.messages, *collected)
    assert collected == tuple(messages)
    assert len(reads) <= len(messages) + 51


@pytest.mark.parametrize("identified", [True, False])
def test_window_retains_complete_tool_exchange_and_compaction_boundary(stores, identified):
    writer, reader = stores
    session_id, prefix = seed(writer, 4)
    run = RunRecord(session_id=session_id, status="completed")
    writer.save_run(run)
    call = ToolCallRequest(id="call", name="Read")
    request = MessageRecord(
        session_id=session_id,
        run_id=run.run_id,
        turn_id="turn" if identified else None,
        role="assistant",
        tool_calls=[call],
    )
    result = MessageRecord(
        session_id=session_id,
        run_id=run.run_id,
        turn_id=request.turn_id,
        role="tool_result",
        tool_result=ToolResultReference(tool_call_id=call.id, tool_name=call.name),
    )
    seed_tool_messages(writer, request, [result], run=run)
    writer.append_messages([request, result])
    compaction = CompactionRecord(
        session_id=session_id,
        strategy_id="test",
        trigger="manual",
        summary="Earlier conversation",
        covered_message_count=len(prefix),
        covered_end_message_id=prefix[-1].message_id,
        history_boundary_message_id=result.message_id,
    )
    from tests.session_summaries import commit_summary

    commit_summary(writer, compaction)
    service = SessionService(reader)
    window = service.history_window(session_id, limit=1)
    expected = (*prefix[-1:], request, result) if identified else (request, result)
    assert window.messages == expected
    assert window.runs == (run,)
    assert window.compactions == (compaction,)
    earlier = service.history_window(
        session_id, before=window.before_cursor, history_epoch=window.history_epoch
    )
    assert (*earlier.messages, *window.messages) == (*prefix, request, result)
    assert not earlier.compactions


def test_window_retains_input_batch_and_rejects_cursor_from_rewritten_history(stores):
    writer, reader = stores
    session_id, messages = seed(writer, 6)
    batch = [
        MessageRecord.text(session_id=session_id, role="user", text=text)
        for text in ("attachment", "question")
    ]
    for message in batch:
        message.metadata["session_input_batch_id"] = "input"
    writer.append_messages(batch)
    service = SessionService(reader)
    window = service.history_window(session_id, limit=1)
    assert window.messages == tuple(batch)
    writer.commit_session(
        SessionCommit(
            before=writer.load_session_record(session_id), before_message_id=messages[1].message_id
        )
    )
    stale = service.history_window(
        session_id, before=window.before_cursor, history_epoch=window.history_epoch
    )
    assert stale.history_epoch == window.history_epoch + 1
    assert not stale.messages and stale.before_cursor is None
    assert service.history_window(session_id).messages == tuple(messages[:1])


async def test_bounded_observation_keeps_active_tail_and_window_policy_on_reset(
    stores, monkeypatch
):
    writer, reader = stores
    session_id, messages = seed(writer, 12)
    run = RunRecord(session_id=session_id, status="running")
    session = writer.load_session_record(session_id)
    writer.commit_session(
        SessionCommit(
            before=session,
            after=session.model_copy(update={"active_run_id": run.run_id, "status": "running"}),
            runs=(run,),
        )
    )

    persist_response_draft(writer, run, turn_id="turn_current", text="still writing")
    run = writer.load_run(session_id, run.run_id)

    def full_history_forbidden(*args, **kwargs):
        pytest.fail("a bounded observation loaded full message history")

    monkeypatch.setattr(reader, "load_session_observation", full_history_forbidden)
    supervisor = supervisor_for(reader)
    observation = await supervisor.observe_session(session_id, history_limit=3)
    try:
        initial = await observation.get()
        assert initial.snapshot.messages == tuple(messages[-3:])
        assert initial.snapshot.events[0].payload["text"] == "still writing"
        assert initial.snapshot.runs == (run,)
        assert initial.snapshot.history_before_cursor is not None
        observation.acknowledge(initial)
        writer.commit_session(
            SessionCommit(
                before=writer.load_session_record(session_id),
                before_message_id=messages[6].message_id,
                events=(
                    EventRecord(session_id=session_id, event_type="session_history_withdrawn"),
                ),
            )
        )
        reset = await asyncio.wait_for(observation.get(), 5)
        assert reset.snapshot.messages == tuple(messages[3:6])
        assert reset.snapshot.history_before_cursor is not None
        assert reset.history_epoch == 1
    finally:
        observation.close()
        await supervisor.shutdown()


async def test_retained_window_survives_resume_but_not_a_history_rewrite(stores):
    writer, reader = stores
    session_id, messages = seed(writer, 12)
    supervisor = supervisor_for(reader)
    observation = await supervisor.observe_session(session_id, history_limit=3)
    try:
        observation.acknowledge(await observation.get())
        observation.retain_history_from(
            SessionHistoryAnchor(history_epoch=0, first_sequence=messages[4].sequence)
        )
        writer.save_session(
            writer.load_session_record(session_id).model_copy(update={"title": "resumed"})
        )
        update = await asyncio.wait_for(observation.get(), 5)
        assert update.snapshot is None
        assert update.facts.session.title == "resumed"
        observation.acknowledge(update)
        writer.commit_session(
            SessionCommit(
                before=writer.load_session_record(session_id),
                before_message_id=messages[2].message_id,
                events=(
                    EventRecord(session_id=session_id, event_type="session_history_withdrawn"),
                ),
            )
        )
        rewritten = await asyncio.wait_for(observation.get(), 5)
        assert rewritten.history_epoch == 1
        assert rewritten.snapshot.messages == tuple(messages[:2])
        assert rewritten.snapshot.history_before_cursor is None
    finally:
        observation.close()
        await supervisor.shutdown()
