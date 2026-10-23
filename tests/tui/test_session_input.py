from __future__ import annotations

import asyncio
import gc
import re
import sys
import threading

import pytest
from rich.control import Control
from textual import events
from textual.geometry import Size
from textual.widgets import Input, Static
from textual.worker import WorkerCancelled

import ouro.tui.conversation.history as conversation_history
import ouro.tui.conversation.markdown_stream as conversation_history_markdown_stream
from ouro.core.events import EventRecord
from ouro.tui.app import OuroTuiApp, RuntimeEventsReady
from ouro.tui.composer.input import Composer
from ouro.tui.conversation.view import ConversationView
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.widgets.status import StatusLine
from tests.tui.helpers import run_ready_app, two_route_application, wait_until


@pytest.mark.parametrize("target", ("message", "blank", "composer"))
async def test_stream_refresh_preserves_pointer_focus_and_terminal_blur(tmp_path, target):
    application = two_route_application(tmp_path)
    session = application.runtime.create_session()
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app, size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        composer = app.query_one(Composer)
        composer.load_text("unsent draft")
        await conversation.add_user("Existing conversation")
        await app._apply_runtime_events(
            [
                EventRecord(
                    session_id=session.session_id,
                    run_id="focus",
                    sequence=1,
                    event_type="run_started",
                    payload={"trigger": "user"},
                )
            ]
        )
        await pilot.pause()
        if target == "message":
            await pilot.click(
                conversation.query_one(conversation_history.ConversationHistory),
                offset=(3, 0),
            )
        elif target == "blank":
            await pilot.click(conversation, offset=(3, conversation.size.height - 1))
        expected = composer if target == "composer" else conversation
        assert app.focused is expected

        async def stream(sequence):
            await app._apply_runtime_events(
                [
                    EventRecord(
                        session_id=session.session_id,
                        run_id="focus",
                        sequence=sequence,
                        event_type="model_text_delta",
                        payload={"text": "More text. "},
                    )
                ]
            )
            await pilot.pause()

        await stream(2)
        assert app.focused is expected
        app.post_message(events.AppBlur())
        await pilot.pause()
        assert not app.app_focus and app.focused is None
        await stream(3)
        assert not app.app_focus and app.focused is None
        assert not composer.has_focus
        app.post_message(events.AppFocus())
        await pilot.pause()
        assert app.focused is expected
        assert composer.text == "unsent draft"


@pytest.mark.parametrize("operation", ["_block_starts", "_render_entry_requests"])
async def test_session_dispatches_input_while_stream_rendering_is_pending(
    tmp_path, monkeypatch, operation
):
    application = two_route_application(tmp_path)
    session = application.runtime.sessions.create(title="Typing while streaming").session
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app, size=(120, 40)) as pilot:
        await app.query_one(ConversationView).add_user("Existing conversation")
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()
        assert not composer.disabled
        assert app.focused is composer
        bounds = composer.region
        started, release = threading.Event(), threading.Event()
        render_module = (
            conversation_history_markdown_stream
            if operation == "_block_starts"
            else conversation_history
        )
        original = getattr(render_module, operation)

        def slow_render(*args, **kwargs):
            started.set()
            assert release.wait(5), "test did not release the background renderer"
            return original(*args, **kwargs)

        monkeypatch.setattr(render_module, operation, slow_render)
        for sequence, (kind, payload) in enumerate(
            (
                ("run_started", {"trigger": "user"}),
                ("model_text_delta", {"text": "```python\n" + "print(1)\n" * 350 + "```\n\nNext"}),
            ),
            start=1,
        ):
            app._queue_runtime_event(
                EventRecord(
                    session_id=session.session_id,
                    run_id="run_input",
                    sequence=sequence,
                    event_type=kind,
                    payload=payload,
                )
            )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            expected = ""
            for key in ("你", "好", "backspace", "界"):
                deleting = key == "backspace"
                expected = expected[:-1] if deleting else expected + key
                app.post_message(events.Key(key, None if deleting else key))
                async with asyncio.timeout(1):
                    while composer.text != expected:
                        await asyncio.sleep(0.01)
            assert composer.region == bounds
            assert app.focused is composer
            assert app.cursor_position == composer.cursor_screen_offset
            # A visible-content handoff keeps frame order while the editor
            # remains responsive; later deltas cannot overtake that transfer.
            app._queue_runtime_event(
                EventRecord(
                    session_id=session.session_id,
                    run_id="run_input",
                    sequence=3,
                    event_type="model_text_delta",
                    payload={"text": " continuation"},
                )
            )
            draining = asyncio.create_task(app._flush_pending_runtime_events())
            await asyncio.sleep(0)
            assert not draining.done()
            assert app._projection is not None and app._projection.last_sequence == 2
        finally:
            release.set()
            await pilot.pause()
        await draining
        assert app._projection.assistant_text.endswith("Next continuation")
        assert app._projection.last_sequence == 3
        assert composer.text == "你界"


async def test_session_status_query_does_not_block_input_dispatch(tmp_path, monkeypatch):
    application = two_route_application(tmp_path)
    session = application.runtime.sessions.create(title="Status refresh").session
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    async with run_ready_app(app, size=(100, 30)) as pilot:
        await app.query_one(ConversationView).add_user("Existing conversation")
        await pilot.pause()
        entered, release = asyncio.Event(), asyncio.Event()

        async def read_interactions():
            entered.set()
            await release.wait()
            return ()

        monkeypatch.setattr(app._controller, "agent_interactions", read_interactions)
        app._queue_runtime_event(
            EventRecord(
                session_id=session.session_id,
                run_id="run_status",
                sequence=1,
                event_type="agent_wait_requested",
                payload={},
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            composer = app.query_one(Composer)
            app.post_message(events.Key("你", "你"))
            async with asyncio.timeout(1):
                while composer.text != "你":
                    await asyncio.sleep(0.01)
            assert app.focused is composer
            assert app.cursor_position == composer.cursor_screen_offset
        finally:
            release.set()
            await pilot.pause()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows terminal output adapter")
async def test_session_frames_preserve_editor_and_menu_cursor_during_live_updates(
    tmp_path, monkeypatch
):
    from textual.drivers.windows_driver import WindowsDriver

    from ouro.tui.windows_driver import OuroWindowsDriver

    writes = []

    def start(driver):
        size = Size(*driver._size)
        driver.send_message(events.Resize(size, size))

    def write(driver, data):
        app = driver._app
        if not app.screen_stack:
            return
        cursor = app.screen.outer_size.clamp_offset(app.cursor_position)
        writes.append((data, Control.move_to(*cursor).segment.text))

    monkeypatch.setattr(WindowsDriver, "start_application_mode", start)
    monkeypatch.setattr(WindowsDriver, "stop_application_mode", lambda _: None)
    monkeypatch.setattr(WindowsDriver, "write", write)
    application = two_route_application(tmp_path)
    session = application.runtime.sessions.create(title="Concurrent presentation").session
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    monkeypatch.setattr(app, "get_driver_class", lambda: OuroWindowsDriver)
    async with run_ready_app(app, headless=False, size=(120, 40)) as pilot:
        conversation = app.query_one(ConversationView)
        await conversation.add_user("Existing conversation")
        for index in range(45):
            await conversation.add_notice(f"History {index}")
        await pilot.pause()
        conversation.scroll_home(animate=False, immediate=True)
        composer = app.query_one(Composer)
        bounds = composer.region
        held_scroll = conversation.scroll_y
        stop = asyncio.Event()

        async def stream():
            sequence = 1
            app._queue_runtime_event(
                EventRecord(
                    session_id=session.session_id,
                    run_id="run_frames",
                    sequence=sequence,
                    event_type="run_started",
                    payload={"trigger": "user"},
                )
            )
            while not stop.is_set():
                sequence += 1
                reasoning = sequence < 6
                text = f"Evidence {sequence} 中文 **answer**.\n\n"
                app._queue_runtime_event(
                    EventRecord(
                        session_id=session.session_id,
                        run_id="run_frames",
                        sequence=sequence,
                        event_type="model_reasoning_delta" if reasoning else "model_text_delta",
                        payload={"reasoning_content" if reasoning else "text": text},
                    )
                )
                app.query_one(StatusLine).set_session_controls(("Plan",) if sequence % 2 else ())
                await asyncio.sleep(0.02)
            return sequence

        writes.clear()
        producing = asyncio.create_task(stream())
        expected = ""
        try:
            for key in ("你", "好", "x", "backspace") * 3:
                deleting = key == "backspace"
                expected = expected[:-1] if deleting else expected + key
                app.post_message(events.Key(key, None if deleting else key))
                async with asyncio.timeout(2):
                    while composer.text != expected:
                        await asyncio.sleep(0.01)
                assert composer.region == bounds
                assert app.cursor_position == composer.cursor_screen_offset
                assert app.focused is composer
                assert conversation.scroll_y == held_scroll
            # Refreshing the transcript must not take focus back from an overlay.
            app.action_actions()
            async with asyncio.timeout(2):
                while not isinstance(app.screen, ChoiceScreen):
                    await asyncio.sleep(0.01)
            await pilot.pause()
            menu = app.screen
            menu_input = menu.query_one(Input)
            assert app.focused is menu_input
            assert app._projection is not None
            previous_sequence = app._projection.last_sequence
            app.refresh(layout=True)
            await wait_until(
                lambda: app._projection.last_sequence > previous_sequence,
                pilot,
                description="stream projection advancing while the menu has focus",
            )
            await pilot.pause()
            assert app.screen is menu and app.focused is menu_input
            assert app.cursor_position == menu_input.cursor_screen_offset
            assert composer.text == expected
        finally:
            stop.set()
            last_sequence = await producing
        await app._flush_pending_runtime_events()
        assert app._projection is not None and app._projection.last_sequence == last_sequence
        # Standalone pointer-shape controls do not move the text cursor.
        frames = [(data, cursor) for data, cursor in writes if re.search(r"\x1b\[\d+;\d+H", data)]
        assert len(frames) > 5
        assert all(data.endswith(cursor) for data, cursor in frames), [
            (data[-100:], cursor) for data, cursor in frames if not data.endswith(cursor)
        ]


async def test_cancelling_event_frame_before_start_does_not_leave_an_unawaited_coroutine(
    tmp_path, recwarn
):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app) as pilot:
        app.on_runtime_events_ready(RuntimeEventsReady())
        worker = app._runtime_events_worker
        assert worker is not None
        worker.cancel()
        with pytest.raises(WorkerCancelled):
            await worker.wait()
        await pilot.pause()
        app._runtime_events_worker = None
        del worker
        gc.collect()
        assert not [
            warning
            for warning in recwarn
            if "_flush_pending_runtime_events" in str(warning.message)
            and "was never awaited" in str(warning.message)
        ]


async def test_cancelling_transcript_removal_preserves_widget_teardown(tmp_path):
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class SlowUnmount(Static):
        async def on_unmount(self):
            entered.set()
            await release.wait()
            finished.set()

    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app) as pilot:
        conversation = app.query_one(ConversationView)
        child = SlowUnmount("Live output")
        await conversation._transcript.mount(child)
        removal = asyncio.create_task(conversation._clear_live_transcript())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            removal.cancel()
            with pytest.raises(asyncio.CancelledError):
                await removal
        finally:
            release.set()
        await asyncio.wait_for(finished.wait(), 2)
        await pilot.pause()
        assert not child.is_attached


async def test_closing_session_cancels_pending_presentation_without_waiting_for_render(
    tmp_path, monkeypatch
):
    application = two_route_application(tmp_path)
    session = application.runtime.sessions.create(title="Close during render").session
    app = OuroTuiApp(application=application, resume_session_id=session.session_id)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = conversation_history_markdown_stream._block_starts

    def slow_parse(source):
        started.set()
        try:
            assert release.wait(5)
            return original(source)
        finally:
            finished.set()

    try:
        async with run_ready_app(app, size=(100, 30)):
            monkeypatch.setattr(conversation_history_markdown_stream, "_block_starts", slow_parse)
            app._queue_runtime_event(
                EventRecord(
                    session_id=session.session_id,
                    run_id="run_close",
                    sequence=1,
                    event_type="model_text_delta",
                    payload={"text": "```\n" + "code\n" * 500},
                )
            )
            assert await asyncio.to_thread(started.wait, 2)
            app.exit()
        assert not finished.is_set()
        assert app._runtime_events_worker is not None and app._runtime_events_worker.is_cancelled
        assert not app._runtime_event_flush_lock.locked()
        assert not app._runtime_event_apply_lock.locked()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
