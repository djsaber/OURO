from __future__ import annotations

import asyncio
import threading

import pytest

from ouro.application.observations.content import active_stream_tail, restore_session_content
from ouro.application.observations.sessions import observation_event
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.state import RunRecord, SessionRecord
from tests.application.test_session_event_following import append, supervisor_for
from tests.application.test_session_event_following import stores as stores


async def collect(observation, through):
    applied = []
    async with asyncio.timeout(8):
        while observation.applied_sequence < through:
            batch = await observation.get()
            assert batch.diagnostic is None, batch.diagnostic
            assert batch.snapshot is None
            before = observation.applied_sequence
            assert before < batch.event_high_water
            assert await observation.get() is batch
            applied.extend(batch.events)
            observation.acknowledge(batch)
    return applied


async def test_snapshot_handoff_replays_beyond_buffer_from_applied_cursor(stores, monkeypatch):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    for index in range(100):
        append(writer, session.session_id, f"{index},")
    supervisor = supervisor_for(reader)
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    read = supervisor.runtime.observation_snapshot
    reads = 0

    def held_snapshot(session_id):
        nonlocal reads
        reads += 1
        snapshot = read(session_id)
        loop.call_soon_threadsafe(entered.set)
        # The test owns this boundary; disk writes must not make it self-release.
        release.wait()
        return snapshot

    monkeypatch.setattr(supervisor.runtime, "observation_snapshot", held_snapshot)
    opening = asyncio.create_task(
        supervisor.observe_session(session.session_id, max_buffer=3, page_size=7)
    )
    try:
        async with asyncio.timeout(60):
            await entered.wait()
            for index in range(100, 120):
                await asyncio.to_thread(append, writer, session.session_id, f"{index},")
        release.set()
        observation = await opening
        initial = await observation.get()
        assert initial.event_high_water == 100
        assert observation.applied_sequence == 0
        assert initial.snapshot is not None
        restore_session_content(initial.snapshot)
        observation.acknowledge(initial)
        applied = await collect(observation, 120)
        assert [event.sequence for event in applied] == list(range(101, 121))
        assert reads == 1
        observation.close()
        assert not supervisor._subscriptions
        assert not supervisor._event_streams
    finally:
        release.set()
        await asyncio.gather(opening, return_exceptions=True)
        await supervisor.shutdown()


async def test_slow_consumer_and_fast_consumer_have_independent_bounded_replay(stores, monkeypatch):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    slow = await supervisor.observe_session(session.session_id, max_buffer=4, page_size=11)
    fast = await supervisor.observe_session(session.session_id, max_buffer=4, page_size=11)
    slow.acknowledge(await slow.get())
    fast.acknowledge(await fast.get())
    limits = []
    read = supervisor.runtime.sessions.observation_events

    def measured(*args, **kwargs):
        limits.append(kwargs["limit"])
        return read(*args, **kwargs)

    monkeypatch.setattr(supervisor.runtime.sessions, "observation_events", measured)
    try:
        fast_events = []
        for burst in range(3):
            for index in range(100):
                last = append(writer, session.session_id, f"{burst}:{index},")
            fast_events.extend(await collect(fast, last.sequence))
        fast.close()
        slow_events = await collect(slow, last.sequence)
        assert [event.sequence for event in slow_events] == [
            event.sequence for event in fast_events
        ]
        assert len(slow_events) == 300
        assert len(limits) > 10 and set(limits) == {11}
        assert session.session_id in supervisor._event_streams
    finally:
        slow.close()
        fast.close()
        await supervisor.shutdown()


async def test_history_change_replaces_snapshot_and_does_not_mix_epochs(stores):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    message = MessageRecord.text(session_id=session.session_id, role="user", text="old")
    commit = writer.commit_session(
        SessionCommit(before=session, steps=(SessionCommitStep(message=message),))
    )
    supervisor = supervisor_for(reader)
    observation = await supervisor.observe_session(session.session_id, max_buffer=1, page_size=2)
    observation.acknowledge(await observation.get())
    try:
        withdrawal = writer.commit_session(
            SessionCommit(
                before=commit.session,
                before_message_id=message.message_id,
                events=(
                    EventRecord(
                        session_id=session.session_id, event_type="session_history_withdrawn"
                    ),
                ),
            )
        )
        reset = await asyncio.wait_for(observation.get(), 5)
        assert reset.snapshot is not None
        assert reset.history_epoch == withdrawal.session.history_epoch == 1
        assert not reset.snapshot.messages
        assert observation.applied_sequence < reset.event_high_water
        observation.acknowledge(reset)
        assert not writer.load_runtime_session(session.session_id).runs
        assert not writer.is_session_locked(session.session_id)
    finally:
        observation.close()
        await supervisor.shutdown()


@pytest.mark.parametrize("max_buffer", [1, 16])
async def test_checkpoint_resume_rebuilds_snapshot_during_live_and_durable_delivery(
    stores, max_buffer
):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    observation = await supervisor.observe_session(
        session.session_id, max_buffer=max_buffer, page_size=2
    )
    observation.acknowledge(await observation.get())
    try:
        # A resume invalidates the old partial turn even within the same epoch.
        for event_type, payload in (
            ("run_status_changed", {"resumed": True, "status": "running"}),
            ("model_request_started", {}),
            ("model_text_delta", {"text": "resumed answer"}),
        ):
            event = writer.append_ordered_event(
                EventRecord(session_id=session.session_id, event_type=event_type, payload=payload)
            )
            await supervisor._on_event(event)
        reset = await asyncio.wait_for(observation.get(), 5)
        assert reset.diagnostic is None
        assert reset.snapshot is not None
        assert reset.history_epoch == session.history_epoch
        assert reset.event_high_water == event.sequence
        assert observation.applied_sequence == 0
        observation.acknowledge(reset)
    finally:
        observation.close()
        await supervisor.shutdown()


async def test_source_failure_survives_full_buffer_and_closes_observation(stores, monkeypatch):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    observation = await supervisor.observe_session(session.session_id, max_buffer=1)
    observation.acknowledge(await observation.get())
    stream = supervisor._event_streams[session.session_id]
    reported = asyncio.Event()
    report = stream._report

    def report_failure(diagnostic):
        report(diagnostic)
        reported.set()

    def fail(*args, **kwargs):
        raise OSError("synthetic source failure")

    stream._report = report_failure
    # Fill the acceleration buffer before failing the shared reader.
    event = append(writer, session.session_id, "buffered")
    stream.offer(event)
    monkeypatch.setattr(reader, "load_events_after", fail)
    stream._wake.set()
    try:
        await asyncio.wait_for(reported.wait(), 5)
        failure = await observation.get()
        assert failure.diagnostic.code == "runtime_subscription_read_failed"
        assert failure.diagnostic.severity == "error"
        assert "OSError" in failure.diagnostic.message
        assert observation.closed
        with pytest.raises(StopAsyncIteration):
            await observation.get()
        assert not supervisor._subscriptions
    finally:
        observation.close()
        await supervisor.shutdown()


async def test_content_restore_keeps_completed_stream_until_canonical_message_exists(stores):
    writer, reader = stores
    session = SessionRecord(active_run_id="run_live")
    writer.create_session(session)
    writer.save_run(RunRecord(session_id=session.session_id, run_id="run_live", status="running"))
    for event_type, payload in (
        ("model_request_started", {"model": "faux"}),
        ("model_reasoning_delta", {"reasoning_content": "thinking"}),
        ("model_text_delta", {"text": "answer"}),
        ("model_message_completed", {}),
    ):
        writer.append_ordered_event(
            EventRecord(
                session_id=session.session_id,
                run_id="run_live",
                turn_id="turn_one",
                event_type=event_type,
                payload=payload,
            )
        )
    supervisor = supervisor_for(reader)
    observation = await supervisor.observe_session(session.session_id)
    try:
        snapshot = (await observation.get()).snapshot
        projection = restore_session_content(snapshot)
        assert projection.assistant_text == "answer"
        assert active_stream_tail(snapshot.events, snapshot.messages) == ("thinking", "answer")
        writer.append_messages(
            [
                MessageRecord.text(
                    session_id=session.session_id,
                    run_id="run_live",
                    turn_id="turn_one",
                    role="assistant",
                    text="answer",
                )
            ]
        )
        second = await supervisor.observe_session(session.session_id)
        try:
            snapshot = (await second.get()).snapshot
            assert active_stream_tail(snapshot.events, snapshot.messages) == ("", "")
            assert not writer.is_session_locked(session.session_id)
        finally:
            second.close()
        old_sequence = projection.last_sequence
        stale = observation_event(
            (
                supervisor.runtime.sessions.observation_events(
                    session.session_id, after_sequence=0, limit=1
                )
            )[0].events[0]
        )
        assert projection.apply(stale) is None
        assert projection.last_sequence == old_sequence
    finally:
        observation.close()
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


async def test_replay_failure_closes_only_failed_consumer_and_preserves_applied_cursor(
    stores, monkeypatch
):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    slow = await supervisor.observe_session(session.session_id, max_buffer=1, page_size=2)
    fast = await supervisor.observe_session(session.session_id, max_buffer=10)
    slow.acknowledge(await slow.get())
    fast.acknowledge(await fast.get())

    def fail(*args, **kwargs):
        raise OSError("page unavailable")

    monkeypatch.setattr(supervisor.runtime.sessions, "observation_events", fail)
    try:
        for _ in range(5):
            event = append(writer, session.session_id, "live")
            await supervisor._on_event(event)
        failure = await slow.get()
        assert failure.diagnostic.code == "session_observation_read_failed"
        assert "OSError" in failure.diagnostic.message
        assert "page unavailable" not in failure.diagnostic.message
        assert slow.applied_sequence == 0
        assert slow.closed
        assert len(await collect(fast, event.sequence)) == 5
        assert not fast.closed
    finally:
        slow.close()
        fast.close()
        await supervisor.shutdown()


async def test_sequence_gaps_are_replayed_without_inventing_events(stores):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    observation = await supervisor.observe_session(session.session_id, max_buffer=1, page_size=2)
    observation.acknowledge(await observation.get())
    try:
        for sequence in (5, 12, 29, 40):
            writer.append_event(
                EventRecord(
                    session_id=session.session_id,
                    sequence=sequence,
                    event_type="model_text_delta",
                    payload={"text": str(sequence)},
                )
            )
        events = await collect(observation, 40)
        assert [event.sequence for event in events] == [5, 12, 29, 40]
    finally:
        observation.close()
        await supervisor.shutdown()


async def test_input_message_is_delivered_with_its_event_in_the_same_history(stores):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    observation = await supervisor.observe_session(session.session_id)
    observation.acknowledge(await observation.get())
    message = MessageRecord.text(session_id=session.session_id, role="user", text="canonical input")
    try:
        writer.commit_session(
            SessionCommit(
                before=session,
                steps=(SessionCommitStep(message=message),),
                events=(
                    EventRecord(
                        session_id=session.session_id,
                        event_type="user_message_added",
                        payload={"message_id": message.message_id},
                    ),
                ),
            )
        )
        batch = await asyncio.wait_for(observation.get(), 5)
        assert batch.diagnostic is None
        assert [item.message_id for item in batch.messages] == [message.message_id]
        assert batch.messages[0].text_content == "canonical input"
        assert observation.applied_sequence == 0
        observation.acknowledge(batch)
        assert observation.applied_sequence == batch.events[-1].sequence
    finally:
        observation.close()
        await supervisor.shutdown()


async def test_application_content_projection_follows_consecutive_runs_without_tui(stores):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    observation = await supervisor.observe_session(session.session_id, max_buffer=2, page_size=3)
    initial = await observation.get()
    projection = restore_session_content(initial.snapshot)
    observation.acknowledge(initial)
    inputs = []
    try:
        for prompt in ("first run", "second run"):
            handle = await supervisor.submit(prompt, session_id=session.session_id)
            result = await supervisor.wait(handle)
            through = reader.latest_event_sequence(session.session_id)
            async with asyncio.timeout(5):
                while observation.applied_sequence < through:
                    batch = await observation.get()
                    assert batch.diagnostic is None
                    for event in batch.events:
                        projection.apply(observation_event(event))
                    inputs.extend(batch.messages)
                    observation.acknowledge(batch)
            assert projection.activity.status == "completed"
            assert projection.activity.run_id == result.run_id
            canonical = supervisor.runtime.sessions.run_result(session.session_id, result.run_id)
            assert projection.assistant_text == canonical.final_text
            supervisor.release_handle(handle)
        assert [message.text_content for message in inputs if message.role == "user"] == [
            "first run",
            "second run",
        ]
    finally:
        observation.close()
        await supervisor.shutdown()


async def test_cancelled_message_lookup_resumes_from_applied_cursor(stores, monkeypatch):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    observation = await supervisor.observe_session(session.session_id)
    observation.acknowledge(await observation.get())
    message = MessageRecord.text(session_id=session.session_id, role="user", text="retained")
    writer.commit_session(
        SessionCommit(
            before=session,
            steps=(SessionCommitStep(message=message),),
            events=(
                EventRecord(
                    session_id=session.session_id,
                    event_type="user_message_added",
                    payload={"message_id": message.message_id},
                ),
            ),
        )
    )
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    read = supervisor.runtime.sessions.messages_at_history

    def held_lookup(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return read(*args, **kwargs)

    monkeypatch.setattr(supervisor.runtime.sessions, "messages_at_history", held_lookup)
    getting = asyncio.create_task(observation.get())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        getting.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await getting
        assert observation.applied_sequence == 0
        batch = await asyncio.wait_for(observation.get(), 5)
        assert batch.diagnostic is None
        assert batch.events[0].data["message_id"] == message.message_id
        assert batch.messages[0].text_content == "retained"
        observation.acknowledge(batch)
    finally:
        release.set()
        observation.close()
        await supervisor.shutdown()
