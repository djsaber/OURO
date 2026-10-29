from __future__ import annotations

import asyncio
import threading

import pytest

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
    message = MessageRecord.text(session_id=session_id, role="user", text=text)
    return store.commit_session(
        SessionCommit(
            before=store.load_session_record(session_id),
            steps=(SessionCommitStep(message=message),),
            events=(
                EventRecord(
                    session_id=session_id,
                    event_type="user_message_added",
                    payload={"message_id": message.message_id},
                ),
            ),
        )
    )


async def facts(subscription):
    async with asyncio.timeout(5):
        while True:
            item = await subscription.get()
            assert item.diagnostic is None, item.diagnostic
            if item.facts is not None:
                return item.facts


async def test_observers_share_bounded_off_loop_reads_and_release_them_on_detach(
    stores, monkeypatch
):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    calls = []
    read = supervisor.runtime.sessions.observation_changes
    loop_thread = threading.get_ident()

    def tracked(*args, **kwargs):
        calls.append((threading.get_ident(), kwargs["limit"]))
        return read(*args, **kwargs)

    monkeypatch.setattr(supervisor.runtime.sessions, "observation_changes", tracked)
    first = await supervisor.subscribe(session.session_id, max_buffer=100)
    second = await supervisor.subscribe(session.session_id, max_buffer=100)
    try:
        assert len(supervisor._event_stream_tasks) == 1
        messages = tuple(
            SessionCommitStep(
                message=MessageRecord.text(
                    session_id=session.session_id, role="user", text=str(index)
                )
            )
            for index in range(300)
        )
        writer.commit_session(SessionCommit(before=session, steps=messages))

        async def read_all(subscription):
            result = []
            while len(result) < 300:
                result.extend((await facts(subscription)).messages)
            return result

        a, b = await asyncio.gather(read_all(first), read_all(second))
        assert a == b
        assert [item.sequence for item in a] == list(range(1, 301))
        assert calls and all(thread != loop_thread and limit == 128 for thread, limit in calls)
        first.close()
        append(writer, session.session_id, "still observed")
        assert (await facts(second)).messages[-1].text_content == "still observed"
        second.close()
        await asyncio.gather(*supervisor._event_stream_tasks)
        assert not supervisor._event_streams
    finally:
        first.close()
        second.close()
        await supervisor.shutdown()


async def test_local_events_arrive_without_per_event_reads(stores, monkeypatch):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    subscription = await supervisor.subscribe(session.session_id)
    try:

        def reject(*args, **kwargs):
            raise AssertionError("local delivery read storage")

        monkeypatch.setattr(supervisor.runtime.sessions, "observation_changes", reject)
        result = append(writer, session.session_id, "immediate")
        await supervisor._on_event(result.events[0])
        await supervisor._on_event(result.events[0])
        item = subscription.get_nowait()
        assert item.event.event_id == result.events[0].event_id
        assert subscription._queue.empty()
    finally:
        subscription.close()
        await supervisor.shutdown()


async def test_follower_failure_isolated_and_reattach_restarts(stores, monkeypatch):
    writer, reader = stores
    session = SessionRecord()
    writer.create_session(session)
    supervisor = supervisor_for(reader)
    read = supervisor.runtime.sessions.observation_changes
    subscription = await supervisor.subscribe(session.session_id)
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise OSError("private details")

    monkeypatch.setattr(supervisor.runtime.sessions, "observation_changes", fail)
    try:
        item = await asyncio.wait_for(subscription.get(), 3)
        assert item.diagnostic.code == "runtime_subscription_read_failed"
        assert "private details" not in item.model_dump_json()
        await asyncio.gather(*supervisor._event_stream_tasks)
        assert calls == [True]
        monkeypatch.setattr(supervisor.runtime.sessions, "observation_changes", read)
        retried = await supervisor.subscribe(session.session_id)
        append(writer, session.session_id, "recovered")
        assert (await facts(retried)).messages[0].text_content == "recovered"
        retried.close()
    finally:
        subscription.close()
        await supervisor.shutdown()


async def test_wait_run_observes_external_terminal_state_without_notification(stores, monkeypatch):
    from ouro.core.base import utc_now

    writer, reader = stores
    run = RunRecord(session_id="ses_external", status="running")
    session = SessionRecord(
        session_id=run.session_id, active_run_id=run.run_id, last_run_id=run.run_id
    )
    writer.create_session(session)
    writer.save_run(run)
    supervisor = supervisor_for(reader)
    waiting = asyncio.create_task(supervisor.wait_run(session.session_id, run.run_id))
    try:
        await asyncio.sleep(0)
        writer.save_run(run.model_copy(update={"status": "completed", "ended_at": utc_now()}))
        assert (await asyncio.wait_for(waiting, 3)).status == "completed"
    finally:
        await supervisor.shutdown()


async def test_activity_reattach_restores_retry_and_follows_external_draft(stores):
    from ouro.core.provider import ProviderRetryNotice
    from ouro.core.response_drafts import ResponseDraftChunk
    from tests.response_drafts import persist_response_draft

    writer, reader = stores
    run = RunRecord(session_id="ses_retry", status="running")
    writer.create_session(SessionRecord(session_id=run.session_id, active_run_id=run.run_id))
    writer.save_run(run)
    header = persist_response_draft(writer, run, turn_id="turn_retry")
    writer.record_request_retry(
        run.session_id,
        header.context_id,
        ProviderRetryNotice(attempt=2, max_attempts=3, delay_seconds=1, reason="network"),
    )
    supervisor = supervisor_for(reader)
    subscription = await supervisor.subscribe(run.session_id, include_activity=True)
    try:
        initial = subscription.initial_activity
        assert initial.activity == "waiting_model"
        assert (initial.model_attempt, initial.model_max_attempts) == (2, 3)
        writer.append_response_draft(
            run.session_id,
            header.context_id,
            ResponseDraftChunk(channel="text", content="live"),
        )
        async with asyncio.timeout(5):
            while True:
                notification = await subscription.get()
                assert notification.diagnostic is None
                if notification.activity and notification.activity.activity == "streaming_text":
                    break
    finally:
        subscription.close()
        await supervisor.shutdown()


async def test_initial_activity_and_position_share_read_boundary(stores, monkeypatch):
    from ouro.core.base import utc_now
    from ouro.core.callbacks import run_blocking

    writer, reader = stores
    run = RunRecord(session_id="ses_boundary", status="running")
    writer.create_session(SessionRecord(session_id=run.session_id, active_run_id=run.run_id))
    writer.save_run(run)
    supervisor = supervisor_for(reader)
    captured, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    load = supervisor._load_activity

    def paused(*args, **kwargs):
        activity = load(*args, **kwargs)
        loop.call_soon_threadsafe(captured.set)
        assert release.wait(5)
        return activity

    monkeypatch.setattr(supervisor, "_load_activity", paused)
    opening = asyncio.create_task(supervisor.subscribe(run.session_id, include_activity=True))
    update = None
    subscription = None
    try:
        await asyncio.wait_for(captured.wait(), 5)
        update = asyncio.create_task(
            run_blocking(
                writer.save_run,
                run.model_copy(update={"status": "completed", "ended_at": utc_now()}),
            )
        )
        await asyncio.sleep(0)
        release.set()
        subscription = await asyncio.wait_for(opening, 5)
        await update
        assert subscription.initial_activity.status == "running"
        async with asyncio.timeout(5):
            while True:
                notification = await subscription.get()
                assert notification.diagnostic is None
                if notification.activity and notification.activity.status == "completed":
                    break
    finally:
        release.set()
        if update is not None:
            await update
        if subscription is not None:
            subscription.close()
        await supervisor.shutdown()


async def test_reattach_discards_cached_activity_from_a_previous_follower(stores):
    from ouro.core.base import utc_now
    from tests.response_drafts import persist_response_draft

    writer, reader = stores
    completed = RunRecord(session_id="ses_reenter", status="completed", ended_at=utc_now())
    writer.create_session(
        SessionRecord(session_id=completed.session_id, last_run_id=completed.run_id)
    )
    writer.save_run(completed)
    supervisor = supervisor_for(reader)
    first = await supervisor.subscribe(completed.session_id, include_activity=True)
    await supervisor._on_event(
        EventRecord(
            session_id=completed.session_id, run_id=completed.run_id, event_type="run_completed"
        )
    )
    assert supervisor._activities[completed.session_id].sequence > 0
    first.close()
    await asyncio.sleep(0)
    active = RunRecord(session_id=completed.session_id, status="running")
    session = writer.load_session_record(active.session_id)
    writer.commit_session(
        SessionCommit(
            before=session,
            after=session.model_copy(update={"active_run_id": active.run_id, "status": "running"}),
            runs=(active,),
        )
    )
    persist_response_draft(writer, active, turn_id="turn_next", text="current answer")
    second = await supervisor.subscribe(active.session_id, include_activity=True)
    try:
        assert second.initial_activity.run_id == active.run_id
        assert supervisor._activities[active.session_id].run_id == active.run_id
        async with asyncio.timeout(5):
            while True:
                item = await second.get()
                assert item.diagnostic is None
                if item.activity and item.activity.activity == "streaming_text":
                    assert item.activity.run_id == active.run_id
                    break
    finally:
        second.close()
        await supervisor.shutdown()
