from __future__ import annotations

import asyncio
import threading

import pytest

import ouro.application.supervisor as supervisor_module
from ouro.application.runtime.service import RuntimeService
from ouro.application.supervisor import RuntimeSupervisor
from ouro.core.adapter import RuntimeAdapter
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.state import RunRecord, SessionRecord
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry


@pytest.fixture(params=["files", "sqlite"])
def stores(tmp_path, request):
    def build():
        store = (
            LocalSessionStore(tmp_path)
            if request.param == "files"
            else SQLiteSessionStore(tmp_path / "sessions.sqlite3")
        )
        store.initialize()
        return store

    return build(), build()


def supervisor_for(store):
    return RuntimeSupervisor(
        RuntimeService(
            store=store,
            tools=ToolRegistry(),
            adapter=RuntimeAdapter(
                FauxProvider(), ModelMetadata(model_id="faux", provider_id="faux")
            ),
        )
    )


def append(store, session_id, text):
    return store.append_ordered_event(
        EventRecord(
            session_id=session_id,
            run_id="run_live",
            event_type="model_text_delta",
            payload={"text": text},
        )
    )


async def events(subscription, count):
    received = []
    async with asyncio.timeout(2):
        while len(received) < count:
            notification = await subscription.get()
            assert notification.kind != "diagnostic", notification
            if notification.event is not None:
                received.append(notification.event)
    return received


async def test_subscription_follows_other_runtime_and_later_events(stores):
    writer, reader = stores
    session = SessionRecord(title="Shared session")
    writer.create_session(session)
    session_id = session.session_id
    first = append(writer, session_id, "before")
    supervisor = supervisor_for(reader)
    subscription = await supervisor.subscribe(session_id, after_sequence=first.sequence)
    try:
        second = append(writer, session_id, "during")
        third = append(writer, session_id, "after")
        received = await events(subscription, 2)
        assert [event.sequence for event in received] == [second.sequence, third.sequence]
        assert [event.text for event in received] == ["during", "after"]
        # Observation remains alive when the first producer stops writing.
        fourth = append(writer, session_id, "later")
        assert (await events(subscription, 1))[0].sequence == fourth.sequence
    finally:
        subscription.close()
        await supervisor.shutdown()


async def test_other_host_observes_undo_and_filters_late_old_epoch_on_reconnect(stores):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    message = MessageRecord.text(session_id=session.session_id, role="user", text="withdraw")
    added = writer.commit_session(
        SessionCommit(before=session, steps=(SessionCommitStep(message=message),))
    )
    supervisor = supervisor_for(reader)
    subscription = await supervisor.subscribe(session.session_id, include_activity=True)
    reconnected = None
    try:
        writer.commit_session(
            SessionCommit(
                before=added.session,
                before_message_id=message.message_id,
                events=(
                    EventRecord(
                        session_id=session.session_id,
                        event_type="session_history_withdrawn",
                        payload={"history_epoch": 1},
                    ),
                ),
            )
        )
        observed = (await events(subscription, 1))[0]
        assert observed.history_epoch == 1
        writer.append_ordered_event(
            EventRecord(
                session_id=session.session_id, event_type="model_request_started", history_epoch=0
            )
        )
        current = writer.append_ordered_event(
            EventRecord(session_id=session.session_id, event_type="diagnostic", history_epoch=1)
        )
        assert (await events(subscription, 1))[0].sequence == current.sequence
        assert supervisor._activities[session.session_id].activity == "idle"
        reconnected = await supervisor.subscribe(session.session_id, include_activity=True)
        assert reconnected.initial_activity.history_epoch == 1
        assert reconnected.initial_activity.activity == "idle"
        replay = await events(reconnected, 2)
        assert [event.event_type for event in replay] == ["session_history_withdrawn", "diagnostic"]
    finally:
        subscription.close()
        if reconnected is not None:
            reconnected.close()
        await supervisor.shutdown()


async def test_on_demand_status_and_new_observer_ignore_an_unfollowed_stale_cache(
    stores, monkeypatch
):
    writer, reader = stores
    session = SessionRecord(title="External continuation")
    run = RunRecord(session_id=session.session_id, status="waiting_approval")
    session = session.model_copy(update={"active_run_id": run.run_id, "last_run_id": run.run_id})
    writer.create_session(session)
    writer.save_run(run)
    supervisor = supervisor_for(reader)
    supervisor._activities[session.session_id] = supervisor._load_activity(session.session_id)
    writer.save_run(run.model_copy(update={"status": "completed"}))
    writer.save_session(session.model_copy(update={"active_run_id": None, "status": "idle"}))

    def no_history_scan(*args, **kwargs):
        raise AssertionError("status queries must read the referenced Run, not all Runs")

    monkeypatch.setattr(reader, "load_runs", no_history_scan)
    try:
        assert supervisor.status(session.session_id).status == "completed"
        assert supervisor.activity(session.session_id).status == "completed"
        subscription = await supervisor.subscribe(session.session_id, include_activity=True)
        assert subscription.initial_activity.status == "completed"
        assert supervisor.activity(session.session_id).status == "completed"
    finally:
        await supervisor.shutdown()


async def test_activity_snapshot_cannot_pair_old_run_state_with_a_new_event_cursor(
    stores, monkeypatch
):
    writer, reader = stores
    session = SessionRecord(title="Atomic activity boundary")
    run = RunRecord(session_id=session.session_id, status="running")
    session = session.model_copy(update={"active_run_id": run.run_id, "last_run_id": run.run_id})
    writer.create_session(session)
    writer.save_run(run)
    first = writer.append_ordered_event(
        EventRecord(
            session_id=session.session_id,
            run_id=run.run_id,
            event_type="run_started",
        )
    )
    captured, release, writing = threading.Event(), threading.Event(), threading.Event()
    read = reader.load_run

    def delayed_run(*args, **kwargs):
        result = read(*args, **kwargs)
        if not captured.is_set():
            captured.set()
            assert release.wait(3)
        return result

    def complete():
        writing.set()
        with writer.session_mutation(session.session_id):
            writer.save_run(run.model_copy(update={"status": "completed"}))
            return writer.append_ordered_event(
                EventRecord(
                    session_id=session.session_id,
                    run_id=run.run_id,
                    event_type="run_completed",
                )
            )

    monkeypatch.setattr(reader, "load_run", delayed_run)
    supervisor = supervisor_for(reader)
    opening = asyncio.create_task(supervisor.subscribe(session.session_id, include_activity=True))
    completion = None
    try:
        assert await asyncio.to_thread(captured.wait, 3)
        completion = asyncio.create_task(asyncio.to_thread(complete))
        assert await asyncio.to_thread(writing.wait, 3)
        if isinstance(reader, SQLiteSessionStore):
            # MVCC permits this commit while the old snapshot remains open.
            await completion
        release.set()
        subscription = await opening
        terminal = await completion
        initial = subscription.initial_activity
        assert initial.status == "running" and initial.sequence == first.sequence
        async with asyncio.timeout(10):
            while True:
                notification = await subscription.get()
                assert notification.kind != "diagnostic", notification
                if (
                    notification.event is not None
                    and notification.event.sequence == terminal.sequence
                ):
                    break
        async with asyncio.timeout(10):
            while supervisor.activity(session.session_id).status != "completed":
                notification = await subscription.get()
                assert notification.kind != "diagnostic", notification
        assert supervisor.activity(session.session_id).sequence == terminal.sequence

        def no_query(*args, **kwargs):
            raise AssertionError("an active subscriber must reuse its maintained projection")

        monkeypatch.setattr(reader, "load_run", no_query)
        for _ in range(100):
            assert supervisor.activity(session.session_id).status == "completed"
    finally:
        release.set()
        await opening
        if completion is not None:
            await completion
        await supervisor.shutdown()


async def test_external_activity_tracks_terminal_state_and_the_next_run(stores):
    writer, reader = stores
    session = SessionRecord(title="Repeated runs")
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    subscription = await supervisor.subscribe(session.session_id, include_activity=True)
    try:
        for run_id in ("run_first", "run_second"):
            for event_type, payload, activity in (
                ("run_started", {}, "preparing_model"),
                ("model_text_delta", {"text": run_id}, "streaming_text"),
                ("run_completed", {}, "completed"),
            ):
                written = writer.append_ordered_event(
                    EventRecord(
                        session_id=session.session_id,
                        run_id=run_id,
                        event_type=event_type,
                        payload=payload,
                    )
                )
                async with asyncio.timeout(2):
                    while True:
                        notification = await subscription.get()
                        assert notification.kind != "diagnostic"
                        if notification.activity is not None:
                            assert notification.activity.run_id == run_id
                            assert notification.activity.sequence == written.sequence
                            assert notification.activity.activity == activity
                            break
    finally:
        subscription.close()
        await supervisor.shutdown()


async def test_local_push_cannot_skip_a_prior_external_event(stores):
    writer, reader = stores
    session = SessionRecord(title="Interleaved writers")
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    subscription = await supervisor.subscribe(session.session_id)
    try:
        external = append(writer, session.session_id, "external")
        local = append(reader, session.session_id, "local")
        await supervisor._on_event(local)
        await supervisor._on_event(local)
        received = await events(subscription, 2)
        assert [event.sequence for event in received] == [external.sequence, local.sequence]
    finally:
        subscription.close()
        await supervisor.shutdown()


async def test_external_append_during_snapshot_handoff_is_not_lost(stores, monkeypatch):
    writer, reader = stores
    session = SessionRecord(title="Snapshot handoff")
    writer.create_session(session)
    first = append(writer, session.session_id, "snapshot")
    captured, release = asyncio.Event(), asyncio.Event()
    run_blocking = supervisor_module.run_blocking

    async def capture_replay(operation, *args, **kwargs):
        result = await run_blocking(operation, *args, **kwargs)
        if operation.__name__ == "read_replay" and not captured.is_set():
            captured.set()
            await asyncio.wait_for(release.wait(), 10)
        return result

    # Hold the snapshot handoff after its storage read scope has ended.
    monkeypatch.setattr(supervisor_module, "run_blocking", capture_replay)
    supervisor = supervisor_for(reader)
    opening = asyncio.create_task(supervisor.subscribe(session.session_id))
    try:
        await asyncio.wait_for(captured.wait(), 10)
        second = append(writer, session.session_id, "during replay")
        release.set()
        subscription = await opening
        assert [event.sequence for event in await events(subscription, 2)] == [
            first.sequence,
            second.sequence,
        ]
    finally:
        release.set()
        await opening
        await supervisor.shutdown()


async def test_durable_sequence_gaps_do_not_stall_following(stores):
    writer, reader = stores
    session = SessionRecord(title="Imported sequence")
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    subscription = await supervisor.subscribe(session.session_id)
    try:
        writer.append_event(
            EventRecord(
                session_id=session.session_id,
                event_type="model_text_delta",
                sequence=10,
                payload={"text": "imported"},
            )
        )
        next_event = append(writer, session.session_id, "next")
        assert [event.sequence for event in await events(subscription, 2)] == [
            10,
            next_event.sequence,
        ]
    finally:
        subscription.close()
        await supervisor.shutdown()


async def test_concurrent_attach_cannot_advance_past_a_slower_subscribers_replay(
    stores, monkeypatch
):
    writer, reader = stores
    session = SessionRecord(title="Concurrent attachment")
    writer.create_session(session)
    first_event = append(writer, session.session_id, "before")
    captured, release = asyncio.Event(), asyncio.Event()
    run_blocking = supervisor_module.run_blocking

    async def delayed_first_replay(operation, *args, **kwargs):
        result = await run_blocking(operation, *args, **kwargs)
        if operation.__name__ == "read_replay" and not captured.is_set():
            captured.set()
            await asyncio.wait_for(release.wait(), 10)
        return result

    monkeypatch.setattr(supervisor_module, "run_blocking", delayed_first_replay)
    supervisor = supervisor_for(reader)
    opening = asyncio.create_task(supervisor.subscribe(session.session_id, max_buffer=100))
    try:
        await asyncio.wait_for(captured.wait(), 10)
        second_event = append(writer, session.session_id, "between observers")
        second = await supervisor.subscribe(session.session_id)
        release.set()
        first = await opening
        for subscription in (first, second):
            assert [event.sequence for event in await events(subscription, 2)] == [
                first_event.sequence,
                second_event.sequence,
            ]
    finally:
        release.set()
        await opening
        await supervisor.shutdown()


async def test_observers_share_bounded_off_loop_reads_and_release_them_on_detach(
    stores, monkeypatch
):
    writer, reader = stores
    session = SessionRecord(title="Shared follower")
    writer.create_session(session)
    calls = []
    read = reader.load_events_after
    loop_thread = threading.get_ident()

    def tracked_read(*args, **kwargs):
        calls.append((threading.get_ident(), kwargs["limit"]))
        return read(*args, **kwargs)

    def reject_full_history(*args, **kwargs):
        raise AssertionError("Live observation must only read incremental pages")

    monkeypatch.setattr(reader, "load_events_after", tracked_read)
    monkeypatch.setattr(reader, "load_session", reject_full_history)
    supervisor = supervisor_for(reader)
    first = await supervisor.subscribe(session.session_id, max_buffer=1000)
    second = await supervisor.subscribe(session.session_id, max_buffer=1000)
    try:
        assert len(supervisor._event_stream_tasks) == 1

        def write_page():
            return [append(writer, session.session_id, str(index)).sequence for index in range(300)]

        sequences = await asyncio.to_thread(write_page)
        assert [event.sequence for event in await events(first, 300)] == sequences
        assert [event.sequence for event in await events(second, 300)] == sequences
        first.close()
        last = append(writer, session.session_id, "still observed")
        assert (await events(second, 1))[0].sequence == last.sequence
        assert all(thread_id != loop_thread and limit <= 1000 for thread_id, limit in calls)
        assert any(limit == 256 for _, limit in calls)
        second.close()
        await asyncio.gather(*supervisor._event_stream_tasks)
        assert not supervisor._event_stream_tasks and not supervisor._event_streams
        count = len(calls)
        append(writer, session.session_id, "unobserved")
        assert len(calls) == count
    finally:
        first.close()
        second.close()
        await supervisor.shutdown()
    assert not supervisor._event_stream_tasks


async def test_local_events_still_arrive_immediately_without_per_event_reads(stores, monkeypatch):
    writer, reader = stores
    session = SessionRecord(title="Local fast path")
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    subscription = await supervisor.subscribe(session.session_id)
    try:

        def reject_read(*args, **kwargs):
            raise AssertionError("Local contiguous pushes must not wait for disk reads")

        # Do not yield to the follower: immediate delivery must work by itself.
        monkeypatch.setattr(reader, "load_events_after", reject_read)
        local = append(reader, session.session_id, "immediate")
        await supervisor._on_event(local)
        await supervisor._on_event(local)
        notification = subscription._queue.get_nowait()
        assert notification.event.sequence == local.sequence
        assert subscription._queue.empty()
    finally:
        subscription.close()
        await supervisor.shutdown()


async def test_follower_storage_failure_is_reported_and_stops_reading(stores, monkeypatch):
    writer, reader = stores
    session = SessionRecord(title="Read failure")
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    subscription = await supervisor.subscribe(session.session_id)
    calls = []

    def fail_read(*args, **kwargs):
        calls.append(True)
        raise OSError("private storage details")

    monkeypatch.setattr(reader, "load_events_after", fail_read)
    try:
        async with asyncio.timeout(2):
            notification = await subscription.get()
        assert notification.diagnostic.code == "runtime_subscription_read_failed"
        assert notification.diagnostic.session_id == session.session_id
        assert "OSError" in notification.diagnostic.message
        assert "private storage details" not in notification.model_dump_json()
        await asyncio.gather(*supervisor._event_stream_tasks)
        assert calls == [True]
    finally:
        subscription.close()
        await supervisor.shutdown()


async def test_follow_failure_during_attach_is_not_relabelled_as_buffer_lag(stores, monkeypatch):
    writer, reader = stores
    session = SessionRecord(title="Failed live handoff")
    writer.create_session(session)
    read = reader.load_events_after

    def fail_follower(*args, **kwargs):
        if kwargs["limit"] == 256:
            raise OSError("Read unavailable")
        return read(*args, **kwargs)

    monkeypatch.setattr(reader, "load_events_after", fail_follower)
    supervisor = supervisor_for(reader)
    subscription = await supervisor.subscribe(session.session_id, max_buffer=100)
    try:
        async with asyncio.timeout(2):
            notification = await subscription.get()
        assert notification.diagnostic.code == "runtime_subscription_read_failed"
    finally:
        subscription.close()
        await supervisor.shutdown()


async def test_explicit_reattach_restarts_following_after_failed_observer_stops(
    stores, monkeypatch
):
    writer, reader = stores
    session = SessionRecord(title="Retry shared observation")
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    read = reader.load_events_after
    existing = await supervisor.subscribe(session.session_id)

    def fail_read(*args, **kwargs):
        raise OSError("Unavailable")

    try:
        monkeypatch.setattr(reader, "load_events_after", fail_read)
        async with asyncio.timeout(2):
            notification = await existing.get()
        assert notification.diagnostic.code == "runtime_subscription_read_failed"
        monkeypatch.setattr(reader, "load_events_after", read)
        retried = await supervisor.subscribe(session.session_id)
        written = append(writer, session.session_id, "recovered")
        assert (await events(retried, 1))[0].sequence == written.sequence
        assert existing.closed
        with pytest.raises(StopAsyncIteration):
            await existing.get()
    finally:
        await supervisor.shutdown()
