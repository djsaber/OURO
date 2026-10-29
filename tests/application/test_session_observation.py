from __future__ import annotations

import asyncio
import threading

import pytest

from ouro.application.observations.content import restore_session_content
from ouro.application.observations.sessions import observation_event
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.response_drafts import ResponseDraftChunk
from ouro.core.session_commits import SessionCommit
from ouro.core.state import RunRecord, SessionRecord
from tests.application.test_session_event_following import append, supervisor_for
from tests.application.test_session_event_following import stores as stores


async def collect(observation, through):
    applied = []
    async with asyncio.timeout(8):
        while observation.applied_position.message_sequence < through:
            batch = await observation.get()
            assert batch.diagnostic is None, batch.diagnostic
            assert batch.snapshot is None
            before = observation.applied_position
            assert await observation.get() is batch
            assert observation.applied_position == before
            applied.extend(batch.messages)
            observation.acknowledge(batch)
    return applied


async def test_snapshot_handoff_fills_overflow_from_applied_position(stores, monkeypatch):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    for index in range(10):
        append(writer, session.session_id, str(index))
    supervisor = supervisor_for(reader)
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    read = supervisor.runtime.observation_snapshot
    reads = []

    def held(session_id):
        reads.append(True)
        snapshot = read(session_id)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        return snapshot

    monkeypatch.setattr(supervisor.runtime, "observation_snapshot", held)
    opening = asyncio.create_task(
        supervisor.observe_session(session.session_id, max_buffer=1, page_size=3)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for index in range(10, 40):
            await asyncio.to_thread(append, writer, session.session_id, str(index))
        release.set()
        observer = await opening
        initial = await observer.get()
        assert initial.position.message_sequence == 10
        assert observer.applied_position.message_sequence == 0
        observer.acknowledge(initial)
        messages = await collect(observer, 40)
        assert [message.sequence for message in messages] == list(range(11, 41))
        assert reads == [True]
        observer.close()
    finally:
        release.set()
        await asyncio.gather(opening, return_exceptions=True)
        await supervisor.shutdown()


async def test_slow_and_fast_observers_have_independent_positions(stores):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    slow = await supervisor.observe_session(session.session_id, max_buffer=1, page_size=3)
    fast = await supervisor.observe_session(session.session_id, max_buffer=100)
    slow.acknowledge(await slow.get())
    fast.acknowledge(await fast.get())
    try:
        for index in range(20):
            result = append(writer, session.session_id, str(index))
            await supervisor._on_event(result.events[0])
        fast_messages, slow_messages = await asyncio.gather(collect(fast, 20), collect(slow, 20))
        assert {m.sequence for m in fast_messages} == set(range(1, 21))
        assert [m.sequence for m in slow_messages] == list(range(1, 21))
        assert fast.applied_position == slow.applied_position
    finally:
        slow.close()
        fast.close()
        await supervisor.shutdown()


@pytest.mark.parametrize("local", [False, True])
async def test_history_change_replaces_only_invalidated_snapshot(stores, local):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    result = append(writer, session.session_id, "withdraw")
    supervisor = supervisor_for(reader)
    observer = await supervisor.observe_session(session.session_id)
    observer.acknowledge(await observer.get())
    try:
        removed = writer.commit_session(
            SessionCommit(
                before=result.session,
                before_message_id=result.messages[0].message_id,
                events=(
                    EventRecord(
                        session_id=session.session_id, event_type="session_history_withdrawn"
                    ),
                ),
            )
        )
        if local:
            await supervisor._on_event(removed.events[-1])
        batch = await asyncio.wait_for(observer.get(), 3)
        assert batch.snapshot is not None
        assert batch.history_epoch == 1
        assert not batch.snapshot.messages
        observer.acknowledge(batch)
        stream = supervisor._event_streams[session.session_id]
        async with asyncio.timeout(3):
            while stream._history_epoch != 1:
                await asyncio.sleep(0)
        assert stream._position.message_sequence == 0
        append(writer, session.session_id, "new history")
        assert (await collect(observer, 1))[0].text_content == "new history"
    finally:
        observer.close()
        await supervisor.shutdown()


async def test_draft_to_message_handoff_keeps_suffix_once(stores):
    from tests.response_drafts import persist_response_draft

    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    run = RunRecord(session_id=session.session_id, status="running")
    writer.save_run(run)
    header = persist_response_draft(writer, run, turn_id="turn_one", text="prefix")
    supervisor = supervisor_for(reader)
    observer = await supervisor.observe_session(session.session_id)
    initial = await observer.get()
    projection = restore_session_content(initial.snapshot)
    observer.acknowledge(initial)
    try:
        assert projection.assistant_text == "prefix"
        writer.append_response_draft(
            session.session_id,
            header.context_id,
            ResponseDraftChunk(channel="text", content=" tail"),
        )
        message = MessageRecord.text(
            session_id=session.session_id,
            run_id=run.run_id,
            turn_id=header.turn_id,
            role="assistant",
            text="prefix tail",
        )
        writer.append_message(message)
        additions = []
        async with asyncio.timeout(5):
            while observer.applied_position.message_sequence < 1:
                batch = await observer.get()
                for item in batch.messages:
                    additions.append(projection.apply_response(item)[0])
                if batch.facts:
                    projection.apply_facts(batch.facts)
                for event in batch.events:
                    update = projection.apply(observation_event(event))
                    if update:
                        additions.append(update.assistant_delta)
                observer.acknowledge(batch)
        assert "".join(additions) == " tail"
        assert projection.assistant_text == "prefix tail"
    finally:
        observer.close()
        await supervisor.shutdown()


async def test_initialization_cancellation_releases_registered_source(stores, monkeypatch):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()

    def held_projection(snapshot):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return snapshot

    monkeypatch.setattr("ouro.application.supervisor.redact_observation", held_projection)
    opening = asyncio.create_task(supervisor.observe_session(session.session_id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert session.session_id in supervisor._subscriptions
        opening.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert not supervisor._subscriptions
        assert not supervisor._event_streams
    finally:
        release.set()
        await supervisor.shutdown()


async def test_local_push_cannot_overtake_an_unseen_message_prefix(stores):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    observer = await supervisor.observe_session(session.session_id, page_size=1)
    observer.acknowledge(await observer.get())
    try:
        append(writer, session.session_id, "external prefix")
        local = append(writer, session.session_id, "local suffix")
        await supervisor._on_event(local.events[0])
        messages = await collect(observer, 2)
        assert [message.text_content for message in messages] == ["external prefix", "local suffix"]
    finally:
        observer.close()
        await supervisor.shutdown()
