import pytest

from ouro.application.events import RuntimeEventRecorder
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

    emitted = []
    sequence = await recorder.emit(
        emitted,
        0,
        session_id=session.session_id,
        run_id="run_1",
        event_type="run_started",
        payload={"trigger": "user"},
    )
    snapshot = store.load_session(session.session_id, recover=False)

    assert sequence == 1
    assert emitted[0].event_type == "run_started"
    assert snapshot.events[0].event_type == "run_started"
    assert seen[0]["event_type"] == "run_started"


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
    emitted = []
    sequence = await recorder.record_hook_failures(emitted, 3, [failure])
    snapshot = store.load_session(session.session_id, recover=False)

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

    emitted = []
    sequence = await recorder.emit(
        emitted,
        0,
        session_id=session.session_id,
        run_id="run_1",
        event_type="run_started",
        payload={},
    )
    snapshot = store.load_session(session.session_id, recover=False)

    assert sequence == 2
    assert [event.event_type for event in emitted] == ["run_started", "plugin_hook_failed"]
    assert [event.event_type for event in snapshot.events] == [
        "run_started",
        "plugin_hook_failed",
    ]
    assert emitted[1].payload["hook_name"] == "on_event"
