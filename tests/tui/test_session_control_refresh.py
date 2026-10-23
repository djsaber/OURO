from __future__ import annotations

import asyncio

import pytest

from ouro.core.events import EventRecord
from ouro.tui.app import OuroTuiApp
from ouro.tui.view_models import TuiSessionControlStatus
from ouro.tui.widgets.activity import ActivityIndicator
from tests.tui.app.providers import BlockingStreamProvider
from tests.tui.helpers import release_on_exit, run_ready_app, wait_for_events
from tests.tui.helpers import wait_for_worker_group as _wait_for_worker_group
from tests.tui.helpers import wait_until as _wait_until
from tests.tui.test_tasks import streaming_application


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_pause_command_stops_blocked_execution_without_a_new_input(tmp_path, backend):
    provider = BlockingStreamProvider()
    application = streaming_application(tmp_path, provider, backend=backend)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app) as pilot:
        await application.plugins.enable("dev.ouro.goal")
        async with release_on_exit(provider.release):
            await app._dispatch_command("/goal Retain this objective")
            await wait_for_events(
                provider.first_chunk_sent, pilot=pilot, description="goal model stream before pause"
            )
            session_id = app._controller.session_id
            before = application.runtime.store.load_session(session_id).messages
            await app._dispatch_command("/goal pause")
            await _wait_for_worker_group(app, pilot, group="plugin-command")
            snapshot = application.runtime.store.load_runtime_session(session_id)
            assert snapshot.runs[-1].status == "aborted"
            assert snapshot.messages == before
            goal = await application.plugins.execute_command(
                "dev.ouro.goal:goal", "status", session_id=session_id
            )
            assert goal.data["goal"]["status"] == "paused"
            assert not provider.release.is_set()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("existing_session", [False, True])
async def test_command_mode_visible_while_first_model_response_is_blocked(
    tmp_path, backend, existing_session
):
    provider = BlockingStreamProvider()
    application = streaming_application(tmp_path, provider, backend=backend)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app) as pilot:
        await application.plugins.enable("dev.ouro.goal")
        if existing_session:
            session = application.runtime.create_session(title="Existing conversation")
            await app._open_session(session.session_id)
        try:
            await app._dispatch_command("/goal Verify immediate mode display")
            await wait_for_events(
                provider.first_chunk_sent,
                pilot=pilot,
                description="goal model stream before finish",
            )
            assert [s.label for s in await app._controller.session_control_statuses()] == ["Goal"]
            await _wait_until(
                lambda: app.query_one(ActivityIndicator)._session_control_labels == ("Goal",),
                pilot,
                description="committed mode before the model finishes",
            )
        finally:
            provider.release.set()
            await _wait_for_worker_group(app, pilot, group="plugin-command")


async def test_slow_command_status_read_does_not_block_model_execution(tmp_path, monkeypatch):
    provider = BlockingStreamProvider()
    application = streaming_application(tmp_path, provider)
    app = OuroTuiApp(application=application)
    entered = asyncio.Event()
    release = asyncio.Event()

    async with run_ready_app(app) as pilot:
        await application.plugins.enable("dev.ouro.goal")
        read_statuses = app._controller.session_control_statuses

        async def delayed_statuses():
            entered.set()
            await release.wait()
            return await read_statuses()

        monkeypatch.setattr(app._controller, "session_control_statuses", delayed_statuses)
        try:
            await app._dispatch_command("/goal Continue while presentation is slow")
            await wait_for_events(
                entered,
                provider.first_chunk_sent,
                pilot=pilot,
                description="model execution while command status read remains blocked",
            )
            assert not release.is_set()
        finally:
            release.set()
            provider.release.set()
            await _wait_for_worker_group(app, pilot, group="plugin-command")


async def test_commands_without_model_input_refresh_mode_on_acquire_and_release(tmp_path):
    provider = BlockingStreamProvider()
    application = streaming_application(tmp_path, provider)
    app = OuroTuiApp(application=application)
    async with run_ready_app(app) as pilot:
        await application.plugins.enable("dev.ouro.plan")
        for command, labels in (("/plan on", ("Plan",)), ("/plan off", ())):
            await app._dispatch_command(command)
            await _wait_for_worker_group(app, pilot, group="plugin-command")
            await _wait_until(
                lambda expected=labels: (
                    app.query_one(ActivityIndicator)._session_control_labels == expected
                ),
                pilot,
                description="mode from command without model input",
            )
        assert not provider.first_chunk_sent.is_set()


async def test_command_submission_after_navigation_does_not_refresh_destination(
    tmp_path, monkeypatch
):
    application = streaming_application(tmp_path, BlockingStreamProvider())
    app = OuroTuiApp(application=application)
    committed = asyncio.Event()
    release = asyncio.Event()
    reads = []
    async with run_ready_app(app) as pilot:
        await application.plugins.enable("dev.ouro.plan")
        execute = application.plugins.execute_command
        read_statuses = app._controller.session_control_statuses

        async def delayed_command(*args, **kwargs):
            result = await execute(*args, **kwargs)
            committed.set()
            await release.wait()
            return result

        async def statuses():
            reads.append(app._controller.session_id)
            return await read_statuses()

        monkeypatch.setattr(application.plugins, "execute_command", delayed_command)
        monkeypatch.setattr(app._controller, "session_control_statuses", statuses)
        try:
            await app._dispatch_command("/plan on")
            await wait_for_events(
                committed, pilot=pilot, description="command commit before navigation"
            )
            await app._new_session()
        finally:
            release.set()
            await _wait_for_worker_group(app, pilot, group="plugin-command")
        assert reads == []
        assert app._controller.session_id is None
        assert app.query_one(ActivityIndicator)._session_control_labels == ()


async def test_pending_status_read_cannot_overwrite_reopened_session(tmp_path, monkeypatch):
    application = streaming_application(tmp_path, BlockingStreamProvider())
    session = application.runtime.create_session(title="Reopened conversation")
    app = OuroTuiApp(application=application)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_statuses():
        entered.set()
        await release.wait()
        return (TuiSessionControlStatus(controller_id="test/control", label="Stale"),)

    async with run_ready_app(app) as pilot:
        await app._open_session(session.session_id)
        monkeypatch.setattr(app._controller, "session_control_statuses", delayed_statuses)
        app._schedule_session_controls_refresh()
        task = app._session_controls_refresh_task
        try:
            await wait_for_events(
                entered, pilot=pilot, description="pending status read before reopening session"
            )
            await app._new_session()
            await app._open_session(session.session_id)
        finally:
            release.set()
            await task
        await pilot.pause()
        assert app.query_one(ActivityIndicator)._session_control_labels == ()


@pytest.mark.parametrize(
    ("event_type", "payload", "expected_reads"),
    [
        ("session_control_transition", {"phase": "applied"}, 1),
        ("session_control_transition", {"phase": "started"}, 0),
        ("session_control_transition", {"phase": "failed"}, 0),
        ("model_text_delta", {"text": "Working"}, 0),
        ("tool_call_completed", {"tool_call_id": "call_test"}, 1),
    ],
)
async def test_control_refresh_uses_committed_events_once(
    tmp_path, monkeypatch, event_type, payload, expected_reads
):
    application = streaming_application(tmp_path, BlockingStreamProvider())
    session = application.runtime.create_session(title="Control events")
    app = OuroTuiApp(application=application)
    reads = 0

    async def statuses():
        nonlocal reads
        reads += 1
        return (TuiSessionControlStatus(controller_id="test/control", label="Review"),)

    async with run_ready_app(app) as pilot:
        await app._open_session(session.session_id)
        monkeypatch.setattr(app._controller, "session_control_statuses", statuses)
        event = EventRecord(
            session_id=session.session_id,
            event_type=event_type,
            sequence=1,
            payload=payload,
        )
        for candidate in (
            event.model_copy(update={"session_id": "another_session"}),
            event.model_copy(update={"history_epoch": session.history_epoch + 1}),
            event.model_copy(update={"sequence": 2}),
            event.model_copy(update={"sequence": 2}),
        ):
            await app._apply_runtime_event_batch((candidate,), input_messages={})
            task = app._session_controls_refresh_task
            if task is not None:
                await task
        await pilot.pause()
        assert reads == expected_reads
