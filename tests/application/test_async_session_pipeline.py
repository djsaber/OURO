from __future__ import annotations

import asyncio
import traceback
from functools import wraps
from threading import Event, get_ident

import pytest

from ouro.application.contracts.agents import SpawnAgentRequest
from ouro.application.contracts.execution import RuntimeNotification
from ouro.application.observations.activity import queued_runtime_activity
from ouro.application.runtime.service import RuntimeService
from ouro.application.supervisor import RuntimeSupervisor
from ouro.core.adapter import RuntimeAdapter
from ouro.core.callbacks import run_blocking
from ouro.core.errors import OuroError, SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.provider import ModelMetadata
from ouro.core.state import SessionRecord
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from tests.application.test_agents import _services
from tests.application.test_compaction import SummaryRouteProvider, _append_turns, _summary_adapter
from tests.application.test_runtime import GateProvider


def _record_loop_store_calls(store, monkeypatch) -> list[str]:
    loop_thread = get_ident()
    calls: list[str] = []

    def instrument(name, operation):
        @wraps(operation)
        def call(*args, **kwargs):
            if get_ident() == loop_thread:
                frame = traceback.extract_stack(limit=3)[-2]
                calls.append(f"{name}: {frame.name}:{frame.lineno}")
            return operation(*args, **kwargs)

        return call

    for name in dir(store):
        if not name.startswith("_") and callable(operation := getattr(store, name)):
            monkeypatch.setattr(store, name, instrument(name, operation))
    return calls


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    store = (
        LocalSessionStore(tmp_path)
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    )
    store.initialize()
    return store


@pytest.fixture
def supervisor(store):
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="faux", provider_id="faux")),
        tools=ToolRegistry(),
    )
    return RuntimeSupervisor(runtime)


async def test_supervised_session_lifecycle_keeps_store_off_loop(supervisor, monkeypatch):
    calls = _record_loop_store_calls(supervisor.store, monkeypatch)
    try:
        handle = await supervisor.submit("first")
        subscription = await supervisor.subscribe(handle.session_id, include_activity=True)
        assert (await supervisor.wait(handle)).status == "completed"
        subscription.close()
        handle = await supervisor.submit("second", session_id=handle.session_id)
        assert (await supervisor.wait(handle)).status == "completed"
        await supervisor.abort(handle.session_id)
    finally:
        await supervisor.shutdown()
    assert calls == [], "\n".join(calls)


async def test_manual_compaction_keeps_store_off_loop(store, monkeypatch):
    runtime = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        system_prompt="",
        adapter=_summary_adapter(SummaryRouteProvider(), provider_id="summary", endpoint_id="chat"),
    )
    session = SessionRecord(title="Compact history")
    store.create_session(session)
    _append_turns(store, session.session_id, 3)
    calls = _record_loop_store_calls(store, monkeypatch)
    try:
        result = await runtime.compact(session_id=session.session_id)
        assert result.run.status == "completed"
        assert result.compaction is not None
    finally:
        await runtime.close()
    assert calls == [], "\n".join(calls)


async def test_queued_input_and_cancellation_keep_store_off_loop(store, monkeypatch):
    provider = GateProvider()
    runtime = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(provider, ModelMetadata(model_id="gate", provider_id="gate")),
    )
    supervisor = RuntimeSupervisor(runtime)
    session = runtime.sessions.create(title="Queued input").session
    calls = _record_loop_store_calls(supervisor.store, monkeypatch)
    try:
        handle = await supervisor.submit("first", session_id=session.session_id)
        await asyncio.wait_for(provider.first_started.wait(), timeout=60)
        await runtime.run("queued", session_id=session.session_id)
        pending = await run_blocking(runtime.pending_user_inputs, session.session_id)
        assert len(pending) == 1
        assert await runtime.cancel_pending_user_input(session.session_id, pending[0].queue_id)
        provider.release_first.set()
        assert (await supervisor.wait(handle)).status == "completed"
    finally:
        provider.release_first.set()
        await supervisor.shutdown()
    assert calls == [], "\n".join(calls)


async def test_agent_lifecycle_and_tree_keep_store_off_loop(tmp_path, monkeypatch):
    service, supervisor, store, _ = await _services(tmp_path)
    calls = _record_loop_store_calls(store, monkeypatch)
    try:
        tree = await service.subscribe_agent_tree("ses_root")
        handle = await service.spawn_agent(
            SpawnAgentRequest(
                request_id="async-boundaries",
                parent_session_id="ses_root",
                parent_run_id="run_parent",
                parent_tool_call_id="spawn_call",
                agent_id="reader",
                task="Read evidence",
                description="Read evidence",
                execution_mode="async",
            )
        )
        await service.wait_agents([handle.delegation_id])
        await service.send_agent_message(
            handle.delegation_id, "follow up", request_id="next", mode="follow_up"
        )
        await service.wait_agents([handle.delegation_id])
        await service.close_agent(handle.delegation_id)
        await service.reconcile_root("ses_root")
        snapshot = await run_blocking(service.agent_tree_snapshot, "ses_root")
        assert snapshot.terminal_count == 1
        tree.close()
    finally:
        await service.close()
        await supervisor.shutdown()
    assert calls == [], "\n".join(calls)


@pytest.mark.parametrize("read_before_live", [False, True])
async def test_subscribe_merges_concurrent_replay_and_live_once(
    supervisor, monkeypatch, read_before_live
):
    store = supervisor.store
    session = SessionRecord(title="Replay race")
    store.create_session(session)
    first = store.append_ordered_event(
        EventRecord(
            session_id=session.session_id, event_type="model_text_delta", payload={"text": "a"}
        )
    )
    entered, release = Event(), Event()
    read = store.load_events_after

    def paused_read(*args, **kwargs):
        result = read(*args, **kwargs) if read_before_live else None
        entered.set()
        assert release.wait(5)
        return result if result is not None else read(*args, **kwargs)

    monkeypatch.setattr(store, "load_events_after", paused_read)
    pending = asyncio.create_task(supervisor.subscribe(session.session_id))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        second = await supervisor.runtime.event_recorder.publish(
            session_id=session.session_id,
            run_id=None,
            event_type="model_text_delta",
            payload={"text": "b"},
        )
        await supervisor.runtime.drain_events()
        release.set()
        subscription = await pending
        # Also cover delayed sink delivery of an event already in replay.
        await supervisor._on_event(second)
        events = [(await subscription.get()).event for _ in range(2)]
        assert [event.sequence for event in events if event] == [first.sequence, second.sequence]
        assert subscription._queue.empty()
        subscription.close()
    finally:
        release.set()
        await pending
        await supervisor.shutdown()


@pytest.mark.parametrize("ending", ["cancel", "shutdown", "read_error"])
async def test_subscription_initialization_failure_detaches(supervisor, monkeypatch, ending):
    store = supervisor.store
    session = SessionRecord(title="Cancel replay")
    store.create_session(session)
    entered, release = Event(), Event()
    read = store.load_events_after
    replay_buffer = 17

    def paused_read(*args, **kwargs):
        # Pause only this subscriber's replay, not the shared live follower.
        # Shutdown must be able to close that independent reader.
        if kwargs["limit"] != replay_buffer:
            return read(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        if ending == "read_error":
            raise OSError("read failed")
        return read(*args, **kwargs)

    monkeypatch.setattr(store, "load_events_after", paused_read)
    pending = asyncio.create_task(
        supervisor.subscribe(session.session_id, max_buffer=replay_buffer)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if ending == "cancel":
            pending.cancel()
            await asyncio.sleep(0)
            pending.cancel()
            assert not pending.done()
        elif ending == "shutdown":
            await supervisor.shutdown()
        release.set()
        error = {"cancel": asyncio.CancelledError, "shutdown": OuroError, "read_error": OSError}[
            ending
        ]
        with pytest.raises(error):
            await pending
        assert session.session_id not in supervisor._subscriptions
    finally:
        release.set()
        await supervisor.shutdown()


async def test_subscription_overflow_during_replay_preserves_lag_diagnostic(
    supervisor, monkeypatch
):
    session = SessionRecord(title="Overflow during replay")
    store = supervisor.store
    store.create_session(session)
    entered, release = Event(), Event()
    read = store.load_events_after

    def paused_read(*args, **kwargs):
        result = read(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(store, "load_events_after", paused_read)
    pending = asyncio.create_task(supervisor.subscribe(session.session_id, max_buffer=2))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(4):
            await supervisor.runtime.event_recorder.publish(
                session_id=session.session_id,
                run_id=None,
                event_type="model_text_delta",
                payload={"text": "live"},
            )
        await supervisor.runtime.drain_events()
        release.set()
        subscription = await pending
        assert (await subscription.get()).kind == "diagnostic"
        assert subscription._queue.empty()
        subscription.close()
    finally:
        release.set()
        await pending
        await supervisor.shutdown()


async def test_replay_does_not_publish_older_live_activity_after_newer_events(
    supervisor, monkeypatch
):
    store = supervisor.store
    session = SessionRecord(title="Activity replay boundary")
    store.create_session(session)
    for text in ("a", "b"):
        store.append_ordered_event(
            EventRecord(
                session_id=session.session_id, event_type="model_text_delta", payload={"text": text}
            )
        )
    entered, release = Event(), Event()
    read = store.load_events_after

    def paused_read(*args, **kwargs):
        result = read(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(store, "load_events_after", paused_read)
    pending = asyncio.create_task(supervisor.subscribe(session.session_id, include_activity=True))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        live = next(iter(supervisor._subscriptions[session.session_id]))
        for sequence in (1, 2):
            live._offer(
                RuntimeNotification(
                    kind="activity",
                    activity=queued_runtime_activity(session.session_id, sequence=sequence),
                )
            )
        release.set()
        subscription = await pending
        notifications = [await subscription.get() for _ in range(3)]
        assert [item.event.sequence for item in notifications if item.event] == [1, 2]
        assert [item.activity.sequence for item in notifications if item.activity] == [2]
        assert subscription._queue.empty()
        subscription.close()
    finally:
        release.set()
        await pending
        await supervisor.shutdown()


@pytest.mark.parametrize("finish_before_second_admission", [False, True])
async def test_concurrent_submissions_respect_session_job_lifetime(
    store, monkeypatch, finish_before_second_admission
):
    provider = GateProvider()
    supervisor = RuntimeSupervisor(
        RuntimeService(
            store=store,
            adapter=RuntimeAdapter(
                provider, ModelMetadata(model_id="gate", provider_id=provider.provider_id)
            ),
            tools=ToolRegistry(),
        )
    )
    session = SessionRecord(title="Concurrent submit")
    supervisor.store.create_session(session)
    enqueue = supervisor._enqueue_job
    arrivals = asyncio.Queue()
    releases = []

    async def held_enqueue(job, **kwargs):
        release = asyncio.Event()
        releases.append(release)
        arrivals.put_nowait((job.handle, release))
        await release.wait()
        await enqueue(job, **kwargs)

    monkeypatch.setattr(supervisor, "_enqueue_job", held_enqueue)
    submissions = []
    try:
        async with asyncio.timeout(60):
            submissions = [
                asyncio.create_task(supervisor.submit("work", session_id=session.session_id))
                for _ in range(2)
            ]
            first, release_first = await arrivals.get()
            second, release_second = await arrivals.get()
            # Both submissions passed preflight before either registered a job.
            # Admission must recheck ownership after its asynchronous reads.
            release_first.set()
            await provider.first_started.wait()
            assert supervisor.status(first).status == "running"
            if finish_before_second_admission:
                provider.release_first.set()
                assert (await supervisor.wait(first)).status == "completed"
            release_second.set()

            results = await asyncio.gather(*submissions, return_exceptions=True)
            handles = [result for result in results if not isinstance(result, BaseException)]
            if finish_before_second_admission:
                assert len(handles) == 2
                assert first in handles and second in handles
                completed = [await supervisor.wait(handle) for handle in handles]
                assert all(snapshot.status == "completed" for snapshot in completed)
                assert len({snapshot.run_id for snapshot in completed}) == 2
            else:
                assert handles == [first]
                assert sum(isinstance(result, SessionBusyError) for result in results) == 1
                assert supervisor.status(first).status == "running"
                provider.release_first.set()
                assert (await supervisor.wait(first)).status == "completed"
    finally:
        for release in releases:
            release.set()
        provider.release_first.set()
        for submission in submissions:
            if not submission.done():
                submission.cancel()
        await asyncio.gather(*submissions, return_exceptions=True)
        await supervisor.shutdown()


async def test_spawn_submission_commit_serializes_with_agent_lifecycle(tmp_path, monkeypatch):
    service, supervisor, store, _ = await _services(tmp_path)
    entered, release = Event(), Event()
    tasks = []
    try:
        handle = await service.spawn_agent(
            SpawnAgentRequest(
                request_id="submission-race",
                parent_session_id="ses_root",
                parent_run_id="run_parent",
                parent_tool_call_id="spawn_call",
                agent_id="reader",
                task="Read",
                description="Read",
                execution_mode="async",
            )
        )
        await service.wait_agents([handle.delegation_id])
        save = store.save_delegation
        contended = asyncio.Event()

        class ObservedLock(asyncio.Lock):
            async def acquire(self):
                if self.locked():
                    contended.set()
                return await super().acquire()

        service._result_locks[handle.delegation_id] = ObservedLock()

        def paused_save(record, **kwargs):
            if record.delegation_id == handle.delegation_id and record.control_state != "closed":
                entered.set()
                assert release.wait(5)
            return save(record, **kwargs)

        monkeypatch.setattr(store, "save_delegation", paused_save)
        tasks.append(asyncio.create_task(service._record_spawn_submission(handle.delegation_id)))
        assert await asyncio.to_thread(entered.wait, 2)
        tasks.append(asyncio.create_task(service.close_agent(handle.delegation_id)))
        await asyncio.wait_for(contended.wait(), 30)
        assert not tasks[1].done()
        release.set()
        await asyncio.gather(*tasks)
        relation = store.load_delegation("ses_root", handle.delegation_id)
        assert relation.spawn_state == "submitted"
        assert relation.control_state == "closed"
        assert relation.result is not None
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await service.close()
        await supervisor.shutdown()
