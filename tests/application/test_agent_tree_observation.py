from __future__ import annotations

import asyncio
import threading

import pytest

from ouro.application.agents.tree import AgentTreeProjector
from ouro.core.events import EventRecord
from ouro.core.state import SessionRecord
from ouro.sessions import SQLiteSessionStore
from tests.application.test_agents import _services
from tests.application.test_session_event_following import supervisor_for


def test_root_tree_facts_share_one_sqlite_read_connection(tmp_path, monkeypatch):
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    store.initialize()
    root = SessionRecord()
    store.create_session(root)
    supervisor = supervisor_for(store)
    connections = []
    original = store._connect

    def connect():
        connection = original()
        connections.append(connection)
        return connection

    monkeypatch.setattr(store, "_connect", connect)
    snapshot = AgentTreeProjector(store=store, runtime=supervisor).snapshot(root.session_id)
    assert snapshot.root_session_id == root.session_id
    assert len(connections) == 1


@pytest.mark.parametrize("count", [65, 256])
async def test_text_burst_is_filtered_before_state_buffer_and_tree_query(
    tmp_path, monkeypatch, count
):
    service, supervisor, _, _ = await _services(tmp_path)
    store = supervisor.store
    loop = asyncio.get_running_loop()
    reading, published = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    read = store.load_events_after
    queries = []
    project = service._tree_projector.snapshot

    def paused_read(*args, **kwargs):
        loop.call_soon_threadsafe(reading.set)
        assert release.wait(10)
        return read(*args, **kwargs)

    def counted_project(*args):
        queries.append(threading.get_ident())
        return project(*args)

    monkeypatch.setattr(store, "load_events_after", paused_read)
    monkeypatch.setattr(service._tree_projector, "snapshot", counted_project)
    subscription = await service.subscribe_agent_tree("ses_root")
    source = supervisor._event_streams["ses_root"]
    publish = source._publish

    def record_publication(event):
        publish(event)
        if event.event_type == "agent_closed":
            published.set()

    source._publish = record_publication
    try:
        await asyncio.wait_for(reading.wait(), 5)
        for index in range(count):
            store.append_ordered_event(
                EventRecord(
                    session_id="ses_root",
                    run_id="run_parent",
                    event_type="model_text_delta",
                    payload={"text": str(index)},
                )
            )
        marker = store.append_ordered_event(
            EventRecord(session_id="ses_root", run_id="run_parent", event_type="agent_closed")
        )
        release.set()
        await asyncio.wait_for(published.wait(), 5)
        # Delivery never computes a tree; even a burst larger than a page is one invalidation.
        assert len(queries) == 1
        notification = await asyncio.wait_for(subscription.get(), 5)
        assert notification.kind == "snapshot", notification
        assert notification.snapshot.revision >= marker.sequence
        assert len(queries) == 2
        assert set(queries).isdisjoint({threading.get_ident()})
    finally:
        release.set()
        subscription.close()
        await service.close()
        await supervisor.shutdown()


async def test_independent_state_observers_share_source_and_outlive_body_subscription(tmp_path):
    service, supervisor, _, _ = await _services(tmp_path)
    first = await service.subscribe_agent_tree("ses_root")
    second = await service.subscribe_agent_tree("ses_root")
    body = await supervisor.subscribe("ses_root")
    source = supervisor._event_streams["ses_root"]
    body.close()
    try:
        await supervisor.runtime.event_recorder.publish(
            session_id="ses_root",
            run_id="run_parent",
            event_type="agent_closed",
            payload={"delegation_id": "del_marker"},
        )
        a = await asyncio.wait_for(first.get(), 5)
        first.close()
        assert supervisor._event_streams["ses_root"] is source
        b = await asyncio.wait_for(second.get(), 5)
        assert a.snapshot == b.snapshot
        pending = asyncio.create_task(second.get())
        second.close()
        with pytest.raises(StopAsyncIteration):
            await pending
        await asyncio.gather(*supervisor._event_stream_tasks)
        assert not supervisor._event_streams
        assert not supervisor._state_subscriptions
    finally:
        first.close()
        second.close()
        await service.close()
        await supervisor.shutdown()


async def test_tree_read_keeps_changes_that_arrive_while_query_is_in_flight(tmp_path, monkeypatch):
    service, supervisor, _, _ = await _services(tmp_path)
    subscription = await service.subscribe_agent_tree("ses_root")
    captured = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    project = service._tree_projector.snapshot
    calls = 0

    def paused_project(session_id):
        nonlocal calls
        calls += 1
        snapshot = project(session_id)
        if calls == 1:
            loop.call_soon_threadsafe(captured.set)
            assert release.wait(10)
        return snapshot

    monkeypatch.setattr(service._tree_projector, "snapshot", paused_project)
    reading = None
    try:
        await supervisor.runtime.event_recorder.publish(
            session_id="ses_root",
            run_id="run_parent",
            event_type="agent_closed",
            payload={"delegation_id": "del_marker"},
        )
        reading = asyncio.create_task(subscription.get())
        await asyncio.wait_for(captured.wait(), 5)
        marker = await supervisor.runtime.event_recorder.publish(
            session_id="ses_root",
            run_id="run_parent",
            event_type="agent_closed",
            payload={"delegation_id": "del_marker"},
        )
        await supervisor.runtime.drain_events()
        release.set()
        previous = await asyncio.wait_for(reading, 5)
        current = await asyncio.wait_for(subscription.get(), 5)
        assert previous.snapshot.revision < current.snapshot.revision
        assert current.snapshot.revision >= marker.sequence
        assert calls == 2
    finally:
        release.set()
        if reading is not None:
            await reading
        subscription.close()
        await service.close()
        await supervisor.shutdown()


async def test_tree_preserves_source_failure_and_stops_observation(tmp_path, monkeypatch):
    service, supervisor, _, _ = await _services(tmp_path)
    subscription = await service.subscribe_agent_tree("ses_root")

    def fail(*args, **kwargs):
        raise OSError("synthetic read failure")

    monkeypatch.setattr(supervisor.store, "load_events_after", fail)
    try:
        notification = await asyncio.wait_for(subscription.get(), 5)
        assert notification.diagnostic.code == "runtime_subscription_read_failed"
        assert notification.diagnostic.severity == "error"
        assert "OSError" in notification.diagnostic.message
        with pytest.raises(StopAsyncIteration):
            await subscription.get()
        assert not supervisor._state_subscriptions
        assert not supervisor._event_streams
    finally:
        subscription.close()
        await service.close()
        await supervisor.shutdown()


async def test_initial_tree_snapshot_retains_changes_during_read(tmp_path, monkeypatch):
    service, supervisor, _, _ = await _services(tmp_path)
    captured, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    project = service._tree_projector.snapshot

    def read(session_id):
        snapshot = project(session_id)
        if not captured.is_set():
            loop.call_soon_threadsafe(captured.set)
            assert release.wait(10)
        return snapshot

    monkeypatch.setattr(service._tree_projector, "snapshot", read)
    opening = asyncio.create_task(service.subscribe_agent_tree("ses_root"))
    try:
        await asyncio.wait_for(captured.wait(), 5)
        marker = await supervisor.runtime.event_recorder.publish(
            session_id="ses_root",
            run_id="run_parent",
            event_type="agent_closed",
            payload={"delegation_id": "del_marker"},
        )
        await supervisor.runtime.drain_events()
        release.set()
        subscription = await opening
        assert subscription.initial_snapshot.revision < marker.sequence
        notification = await asyncio.wait_for(subscription.get(), 5)
        assert notification.snapshot.revision >= marker.sequence
        subscription.close()
    finally:
        release.set()
        await opening
        await service.close()
        await supervisor.shutdown()


async def test_failed_tree_projection_stops_only_that_observer(tmp_path, monkeypatch):
    service, supervisor, _, _ = await _services(tmp_path)
    first = await service.subscribe_agent_tree("ses_root")
    second = await service.subscribe_agent_tree("ses_root")
    project = service._tree_projector.snapshot
    calls = 0

    def fail_once(session_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("synthetic projection failure")
        return project(session_id)

    monkeypatch.setattr(service._tree_projector, "snapshot", fail_once)
    try:
        await supervisor.runtime.event_recorder.publish(
            session_id="ses_root",
            run_id="run_parent",
            event_type="agent_closed",
            payload={"delegation_id": "del_marker"},
        )
        failed = await asyncio.wait_for(first.get(), 5)
        assert failed.diagnostic.code == "agent_tree_subscription_failed"
        assert "ValueError" in failed.diagnostic.message
        with pytest.raises(StopAsyncIteration):
            await first.get()
        assert (await asyncio.wait_for(second.get(), 5)).snapshot is not None
    finally:
        first.close()
        second.close()
        await service.close()
        await supervisor.shutdown()


@pytest.mark.parametrize("cancel", [False, True])
async def test_initial_tree_read_failure_or_cancellation_releases_sources(
    tmp_path, monkeypatch, cancel
):
    service, supervisor, _, _ = await _services(tmp_path)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = service._tree_projector.snapshot

    def read(session_id):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10)
        if not cancel:
            raise ValueError("synthetic projection failure")
        return original(session_id)

    monkeypatch.setattr(service._tree_projector, "snapshot", read)
    opening = asyncio.create_task(service.subscribe_agent_tree("ses_root"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if cancel:
            opening.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError if cancel else ValueError):
            await opening
        await asyncio.gather(*supervisor._event_stream_tasks)
        assert not supervisor._state_subscriptions
        assert not supervisor._event_streams
        assert not service._tree_subscriptions
    finally:
        release.set()
        await service.close()
        await supervisor.shutdown()
