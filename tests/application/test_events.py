import asyncio
import threading

import pytest

from ouro.application.events import RuntimeEventRecorder
from ouro.application.observers import ObserverDispatcher
from ouro.core.events import EventRecord
from ouro.core.observers import (
    ObserverFailure,
    ObserverRegistration,
    ObserverRegistry,
    RuntimeEventObservation,
)
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore


@pytest.mark.asyncio
async def test_event_recorder_persists_event_and_dispatches_runtime_event_observer(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Events")
    store.create_session(session)
    observers = ObserverRegistry()
    seen: list[str] = []

    def on_event(observation: RuntimeEventObservation) -> None:
        seen.append(observation.record.event_type)

    observers.register(
        ObserverRegistration(
            plugin_id="observer",
            observer_id="events",
            event="runtime_event",
            handler=on_event,
        )
    )
    recorder = RuntimeEventRecorder(store=store, observers=observers)

    emitted: list[EventRecord] = []
    sequence = await recorder.emit(
        emitted,
        0,
        session_id=session.session_id,
        run_id="run_1",
        event_type="run_started",
        payload={"trigger": "user"},
    )
    await recorder.close()
    snapshot = store.load_session(session.session_id)

    assert sequence == 1
    assert emitted[0].event_type == "run_started"
    assert snapshot.events[0].event_type == "run_started"
    assert seen == ["run_started"]


@pytest.mark.asyncio
async def test_runtime_event_observer_receives_a_redacted_payload(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Redacted events")
    store.create_session(session)
    observers = ObserverRegistry()
    seen: list[object] = []

    def observe(observation: RuntimeEventObservation) -> None:
        seen.append(observation.record.payload["text"])

    observers.register(
        ObserverRegistration(
            plugin_id="observer",
            observer_id="events",
            event="runtime_event",
            handler=observe,
        )
    )
    recorder = RuntimeEventRecorder(store=store, observers=observers)

    await recorder.emit(
        [],
        0,
        session_id=session.session_id,
        run_id="run_1",
        event_type="model_text_delta",
        payload={"text": "token=observer-secret"},
    )
    await recorder.close()

    assert seen == ["token=***"]
    assert store.load_session(session.session_id).events[0].payload["text"] == (
        "token=observer-secret"
    )


@pytest.mark.asyncio
async def test_application_event_publish_uses_same_durable_observer_boundary(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Application events")
    store.create_session(session)
    seen: list[EventRecord] = []
    recorder = RuntimeEventRecorder(
        store=store,
        observers=ObserverRegistry(),
        event_sink=seen.append,
    )

    event = await recorder.publish(
        session_id=session.session_id,
        run_id="run_parent",
        event_type="agent_closed",
        payload={"delegation_id": "dlg_child"},
    )
    with pytest.raises(ValueError, match="child_session_id"):
        await recorder.publish(
            session_id=session.session_id,
            run_id="run_parent",
            event_type="agent_spawned",
            payload={"delegation_id": "dlg_invalid"},
        )
    with pytest.raises(ValueError, match="parent_tool_call_id"):
        await recorder.publish(
            session_id=session.session_id,
            run_id="run_parent",
            event_type="agent_spawned",
            payload={
                "delegation_id": "dlg_invalid_parent",
                "child_session_id": "ses_child",
                "child_run_id": "run_child",
                "agent_id": "reader",
                "execution_mode": "async",
                "root_session_id": session.session_id,
                "parent_session_id": session.session_id,
                "parent_run_id": "run_parent",
                "parent_tool_call_id": 42,
            },
        )
    await recorder.close()

    assert event.sequence == 1
    assert store.load_session(session.session_id).events == [event]
    assert seen == [event]


@pytest.mark.asyncio
async def test_event_observer_retains_registry_resolved_at_publish_time(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Catalog-bound events")
    store.create_session(session)
    first_observers = ObserverRegistry()
    second_observers = ObserverRegistry()
    seen: list[str] = []
    releases: list[str] = []
    first_observers.register(
        ObserverRegistration(
            plugin_id="first",
            observer_id="events",
            event="runtime_event",
            handler=lambda _context: seen.append("first"),
        )
    )
    second_observers.register(
        ObserverRegistration(
            plugin_id="second",
            observer_id="events",
            event="runtime_event",
            handler=lambda _context: seen.append("second"),
        )
    )
    active_observers = first_observers
    recorder = RuntimeEventRecorder(store=store, observers=ObserverRegistry())

    def resolve(_observation):
        retained = active_observers
        return retained, lambda: releases.append("released")

    recorder.set_observer_registry_resolver(resolve)
    await recorder.emit(
        [],
        0,
        session_id=session.session_id,
        run_id="run_catalog",
        event_type="run_started",
        payload={"trigger": "user"},
    )
    active_observers = second_observers
    await recorder.close()

    assert seen == ["first"]
    assert releases == ["released"]


@pytest.mark.asyncio
async def test_event_recorder_records_observer_failures_as_events(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Observer failures")
    store.create_session(session)
    recorder = RuntimeEventRecorder(store=store, observers=ObserverRegistry())

    failure = ObserverFailure(
        plugin_id="bad",
        observer_id="tools",
        observation="tool_call_finished",
        session_id=session.session_id,
        run_id="run_1",
        error_type="RuntimeError",
        message="boom",
    )
    await recorder.record_observer_failure(failure)
    await recorder.close()
    snapshot = store.load_session(session.session_id)

    assert snapshot.events[0].event_type == "plugin_observer_failed"
    assert snapshot.events[0].payload["plugin_id"] == "bad"


@pytest.mark.asyncio
async def test_event_recorder_converts_observer_failure_to_failure_event(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Event observer failure")
    store.create_session(session)
    observers = ObserverRegistry()

    def broken_on_event(_observation) -> None:
        raise RuntimeError("observer exploded")

    observers.register(
        ObserverRegistration(
            plugin_id="broken",
            observer_id="events",
            event="runtime_event",
            handler=broken_on_event,
        )
    )
    recorder = RuntimeEventRecorder(store=store, observers=observers)

    emitted: list[EventRecord] = []
    sequence = await recorder.emit(
        emitted,
        0,
        session_id=session.session_id,
        run_id="run_1",
        event_type="run_started",
        payload={},
    )
    await recorder.close()
    snapshot = store.load_session(session.session_id)

    assert sequence == 1
    assert [event.event_type for event in emitted] == ["run_started"]
    assert [event.event_type for event in snapshot.events] == [
        "run_started",
        "plugin_observer_failed",
    ]
    assert snapshot.events[1].payload["observer_id"] == "events"


@pytest.mark.asyncio
async def test_event_recorder_allocates_unique_sequences_across_concurrent_emitters(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Concurrent events")
    store.create_session(session)
    sink_entered = asyncio.Event()
    release_sink = asyncio.Event()

    async def blocking_sink(event: EventRecord) -> None:
        del event
        sink_entered.set()
        await release_sink.wait()

    first = RuntimeEventRecorder(
        store=store,
        observers=ObserverRegistry(),
        event_sink=blocking_sink,
    )
    second = RuntimeEventRecorder(store=store, observers=ObserverRegistry())
    first_events: list[EventRecord] = []
    second_events: list[EventRecord] = []

    first_emit = asyncio.create_task(
        first.emit(
            first_events,
            0,
            session_id=session.session_id,
            run_id="run_1",
            event_type="run_started",
            payload={},
        )
    )
    await sink_entered.wait()
    second_sequence = await second.emit(
        second_events,
        0,
        session_id=session.session_id,
        run_id="run_1",
        event_type="model_request_started",
        payload={},
    )
    release_sink.set()
    first_sequence = await first_emit
    await first.close()
    await second.close()

    snapshot = store.load_session(session.session_id)
    assert [event.sequence for event in snapshot.events] == [1, 2]
    assert first_events[0].sequence == 1
    assert second_events[0].sequence == 2
    assert first_sequence == 1
    assert second_sequence == 2


@pytest.mark.asyncio
async def test_slow_file_persistence_does_not_block_another_session(tmp_path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingStore(LocalSessionStore):
        blocked_session_id = ""

        def append_ordered_event(self, event: EventRecord) -> EventRecord:
            if event.session_id == self.blocked_session_id:
                entered.set()
                assert release.wait(timeout=2)
            return super().append_ordered_event(event)

    store = BlockingStore(tmp_path)
    store.initialize()
    blocked = SessionRecord(title="Blocked")
    independent = SessionRecord(title="Independent")
    store.create_session(blocked)
    store.create_session(independent)
    store.blocked_session_id = blocked.session_id
    recorder = RuntimeEventRecorder(store=store, observers=ObserverRegistry())

    blocked_emit = asyncio.create_task(
        recorder.emit(
            [],
            0,
            session_id=blocked.session_id,
            run_id="run_blocked",
            event_type="run_started",
            payload={},
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)
    independent_sequence = await asyncio.wait_for(
        recorder.emit(
            [],
            0,
            session_id=independent.session_id,
            run_id="run_independent",
            event_type="run_started",
            payload={},
        ),
        timeout=0.5,
    )
    release.set()
    await blocked_emit
    await recorder.close()

    assert independent_sequence == 1


@pytest.mark.asyncio
async def test_observer_timeout_cannot_block_durable_event(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Observer timeout")
    store.create_session(session)
    observers = ObserverRegistry()

    async def never_returns(_observation) -> None:
        await asyncio.Event().wait()

    observers.register(
        ObserverRegistration(
            plugin_id="hung-observer",
            observer_id="events",
            event="runtime_event",
            handler=never_returns,
            timeout_seconds=0.02,
        )
    )
    recorder = RuntimeEventRecorder(store=store, observers=observers)

    sequence = await asyncio.wait_for(
        recorder.emit(
            [],
            0,
            session_id=session.session_id,
            run_id="run_1",
            event_type="run_started",
            payload={},
        ),
        timeout=0.5,
    )
    await recorder.close()

    assert sequence == 1
    snapshot = store.load_session(session.session_id)
    assert [event.event_type for event in snapshot.events] == [
        "run_started",
        "plugin_observer_failed",
    ]
    assert snapshot.events[1].payload["error_type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_event_recorder_close_applies_observer_shutdown_deadline(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Observer shutdown")
    store.create_session(session)
    observers = ObserverRegistry()
    started = asyncio.Event()

    async def never_returns(_observation) -> None:
        started.set()
        await asyncio.Event().wait()

    observers.register(
        ObserverRegistration(
            plugin_id="hung-observer",
            observer_id="events",
            event="runtime_event",
            handler=never_returns,
            timeout_seconds=30,
        )
    )
    recorder = RuntimeEventRecorder(
        store=store,
        observers=observers,
        observer_dispatcher=ObserverDispatcher(shutdown_timeout_seconds=0.01),
    )

    await recorder.emit(
        [],
        0,
        session_id=session.session_id,
        run_id="run_1",
        event_type="run_started",
        payload={},
    )
    await asyncio.wait_for(started.wait(), timeout=0.2)

    await asyncio.wait_for(recorder.close(), timeout=0.2)


@pytest.mark.asyncio
async def test_stable_runtime_event_payload_is_validated_before_persistence(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Invalid event")
    store.create_session(session)
    recorder = RuntimeEventRecorder(store=store, observers=ObserverRegistry())

    with pytest.raises(ValueError, match="requires string payload field text"):
        await recorder.emit(
            [],
            0,
            session_id=session.session_id,
            run_id="run_1",
            event_type="model_text_delta",
            payload={},
        )

    await recorder.close()
    assert store.load_session(session.session_id).events == []


@pytest.mark.asyncio
async def test_tool_output_with_malformed_url_does_not_break_event_delivery(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Untrusted tool output")
    store.create_session(session)
    recorder = RuntimeEventRecorder(store=store, observers=ObserverRegistry())
    seen: list[str] = []
    recorder.add_event_sink(lambda event: seen.append(event.event_type))

    emitted = []
    sequence = await recorder.emit(
        emitted,
        0,
        session_id=session.session_id,
        run_id="run_1",
        event_type="tool_call_completed",
        payload={
            "tool_call_id": "call_1",
            "result_data": {
                "content": (
                    "https://example.com:i=5e3}n;"
                    "if(window.crsRequestData={url:r.toString()},i>=0){let"
                )
            },
        },
    )
    await recorder.drain()

    assert sequence == 1
    assert [event.event_type for event in emitted] == ["tool_call_completed"]
    assert seen == ["tool_call_completed"]
    assert not any(
        event.event_type == "event_sink_failed"
        for event in store.load_session(session.session_id).events
    )
    await recorder.close()


@pytest.mark.asyncio
async def test_host_event_sink_registration_detaches_without_closing_runtime(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Detachable host")
    store.create_session(session)
    recorder = RuntimeEventRecorder(store=store, observers=ObserverRegistry())
    seen: list[str] = []
    registration = recorder.add_event_sink(lambda event: seen.append(event.event_type))

    await recorder.publish(
        session_id=session.session_id,
        run_id="run_1",
        event_type="run_started",
        payload={"trigger": "user"},
    )
    await recorder.drain()
    await registration.close()
    await registration.close()
    await recorder.publish(
        session_id=session.session_id,
        run_id="run_1",
        event_type="run_completed",
        payload={},
    )
    await recorder.drain()

    assert seen == ["run_started"]
    assert [event.event_type for event in store.load_session(session.session_id).events] == [
        "run_started",
        "run_completed",
    ]
    await recorder.close()


@pytest.mark.asyncio
async def test_host_sink_worker_starts_with_eager_task_factory(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Eager host")
    store.create_session(session)
    recorder = RuntimeEventRecorder(store=store, observers=ObserverRegistry())
    seen: list[str] = []

    async def capture(event: EventRecord) -> None:
        seen.append(event.event_type)

    recorder.add_event_sink(capture)
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        await recorder.emit(
            [],
            0,
            session_id=session.session_id,
            run_id="run_eager",
            event_type="run_started",
            payload={"trigger": "user"},
        )
        await asyncio.wait_for(recorder.drain(), timeout=0.5)

        assert seen == ["run_started"]
    finally:
        loop.set_task_factory(previous_factory)
        await asyncio.wait_for(recorder.close(), timeout=0.5)
