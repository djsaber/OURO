import asyncio
import threading

import pytest

from ouro.application.events import RuntimeEventRecorder
from ouro.core.events import EventRecord
from ouro.core.hooks import HookContext, HookFailure, HookRegistration, HookRegistry, HookResult
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore


@pytest.mark.asyncio
async def test_event_recorder_persists_event_and_dispatches_on_event_hook(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Events")
    store.create_session(session)
    hooks = HookRegistry()
    seen: list[dict[str, object]] = []

    def on_event(context: HookContext) -> HookResult:
        seen.append(context.data["event"])
        return HookResult()

    hooks.register(
        HookRegistration(
            plugin_id="observer",
            hook_name="on_event",
            handler=on_event,
        )
    )
    recorder = RuntimeEventRecorder(store=store, hooks=hooks)

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
    assert seen[0]["event_type"] == "run_started"


@pytest.mark.asyncio
async def test_event_observer_retains_hook_registry_resolved_at_publish_time(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Catalog-bound events")
    store.create_session(session)
    first_hooks = HookRegistry()
    second_hooks = HookRegistry()
    seen: list[str] = []
    releases: list[str] = []
    first_hooks.register(
        HookRegistration(
            plugin_id="first",
            hook_name="on_event",
            handler=lambda _context: seen.append("first"),
        )
    )
    second_hooks.register(
        HookRegistration(
            plugin_id="second",
            hook_name="on_event",
            handler=lambda _context: seen.append("second"),
        )
    )
    active_hooks = first_hooks
    recorder = RuntimeEventRecorder(store=store, hooks=HookRegistry())

    def resolve(_run_id: str | None):
        retained = active_hooks
        return retained, lambda: releases.append("released")

    recorder.set_hook_registry_resolver(resolve)
    await recorder.emit(
        [],
        0,
        session_id=session.session_id,
        run_id="run_catalog",
        event_type="run_started",
        payload={"trigger": "user"},
    )
    active_hooks = second_hooks
    await recorder.close()

    assert seen == ["first"]
    assert releases == ["released"]


@pytest.mark.asyncio
async def test_event_recorder_records_hook_failures_as_events(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Hook failures")
    store.create_session(session)
    recorder = RuntimeEventRecorder(store=store, hooks=HookRegistry())

    failure = HookFailure(
        plugin_id="bad",
        hook_name="after_tool_call",
        session_id=session.session_id,
        run_id="run_1",
        error_type="RuntimeError",
        message="boom",
    )
    emitted: list[EventRecord] = []
    sequence = await recorder.record_hook_failures(emitted, 3, [failure])
    await recorder.close()
    snapshot = store.load_session(session.session_id)

    assert sequence == 4
    assert emitted[0].event_type == "plugin_hook_failed"
    assert emitted[0].sequence == 4
    assert emitted[0].payload["plugin_id"] == "bad"
    assert snapshot.events[0].event_type == "plugin_hook_failed"


@pytest.mark.asyncio
async def test_event_recorder_converts_on_event_hook_failure_to_failure_event(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Event hook failure")
    store.create_session(session)
    hooks = HookRegistry()

    def broken_on_event(context: HookContext) -> HookResult:
        raise RuntimeError("on_event exploded")

    hooks.register(
        HookRegistration(
            plugin_id="broken",
            hook_name="on_event",
            handler=broken_on_event,
        )
    )
    recorder = RuntimeEventRecorder(store=store, hooks=hooks)

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
        "plugin_hook_failed",
    ]
    assert snapshot.events[1].payload["hook_name"] == "on_event"


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
        hooks=HookRegistry(),
        event_sink=blocking_sink,
    )
    second = RuntimeEventRecorder(store=store, hooks=HookRegistry())
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
    recorder = RuntimeEventRecorder(store=store, hooks=HookRegistry())

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
async def test_observational_hook_timeout_cannot_block_durable_event(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Hook timeout")
    store.create_session(session)
    hooks = HookRegistry(observational_timeout_seconds=0.02)

    async def never_returns(context: HookContext) -> HookResult:
        del context
        await asyncio.Event().wait()
        return HookResult()

    hooks.register(
        HookRegistration(
            plugin_id="hung-observer",
            hook_name="on_event",
            handler=never_returns,
        )
    )
    recorder = RuntimeEventRecorder(store=store, hooks=hooks)

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
        "plugin_hook_failed",
    ]
    assert snapshot.events[1].payload["error_type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_stable_runtime_event_payload_is_validated_before_persistence(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Invalid event")
    store.create_session(session)
    recorder = RuntimeEventRecorder(store=store, hooks=HookRegistry())

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
