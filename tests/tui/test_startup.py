"""Startup terminal ownership, animation lifetime and offline recovery handoff."""

import asyncio
import os
from contextlib import contextmanager
from io import StringIO
from threading import Event, current_thread
from threading import enumerate as threads

import pytest
from textual import events
from textual.app import App
from textual.drivers.headless_driver import HeadlessDriver
from textual.widgets import Static

from ouro.core.errors import UnsupportedDataVersionError
from ouro.tui.app import OuroTuiApp
from ouro.tui.entry import run_tui
from ouro.tui.startup import TerminalStartup
from ouro.tui.startup_driver import startup_driver
from ouro.tui.startup_screen import StartupScreen
from ouro_upgrade.workspace import Workspace
from tests.tui.helpers import two_route_application


def test_redirected_startup_does_not_emit_terminal_controls(monkeypatch):
    output = StringIO()
    monkeypatch.setattr("sys.stdout", output)
    with TerminalStartup() as feedback:
        feedback.stop()
    assert output.getvalue() == ""


def test_first_frame_and_animation_stop_before_terminal_handoff(monkeypatch):
    advanced = Event()

    class Terminal(StringIO):
        def isatty(self):
            return True

        def fileno(self):
            return 1

        def flush(self):
            if current_thread().name == "ouro-startup":
                advanced.set()

    output = Terminal()
    monkeypatch.setattr("sys.stdout", output)
    monkeypatch.setattr("sys.stdin", output)
    monkeypatch.setattr("os.get_terminal_size", lambda _: os.terminal_size((80, 24)))
    with TerminalStartup() as feedback:
        assert output.getvalue()
        assert advanced.wait(5)
        feedback.stop()
        assert not any(thread.name == "ouro-startup" for thread in threads())
    assert output.getvalue().endswith("\x1b[?25h")


async def test_terminal_handoff_preserves_first_frame_but_resume_enters_alternate_screen(
    monkeypatch,
):
    output = StringIO()
    monkeypatch.setattr(output, "isatty", lambda: True)
    monkeypatch.setattr(output, "fileno", lambda: 1)
    monkeypatch.setattr("sys.stdout", output)
    monkeypatch.setattr("sys.stdin", output)
    monkeypatch.setattr("os.get_terminal_size", lambda _: os.terminal_size((80, 24)))
    writes = []

    class PlatformDriver(HeadlessDriver):
        def start_application_mode(self):
            assert not feedback.active
            self.write("\x1b[?1049h")
            self.write("\x1b[?25l")

        def write(self, text):
            writes.append(text)

    with TerminalStartup() as feedback:
        driver = startup_driver(PlatformDriver, feedback)(App())
        driver.start_application_mode()
        assert writes == ["\x1b[?25l"]
        writes.clear()
        driver.start_application_mode()
        assert writes == ["\x1b[?1049h", "\x1b[?25l"]


async def test_startup_animation_stops_on_failure_and_unmount():
    app = App()
    screen = StartupScreen()
    async with app.run_test(size=(40, 12)) as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        screen.show_error("Cannot open data")
        assert "Cannot open data" in screen.query_one("#startup-message", Static).render().plain
        assert screen._timer._task is None
        await app.pop_screen()
        assert screen._timer._task is None


@pytest.mark.parametrize("key", ["ctrl+c", "ctrl+q"])
async def test_startup_exit_keeps_feedback_visible_until_owned_construction_settles(
    tmp_path, monkeypatch, key
):
    application = two_route_application(tmp_path)
    entered, release = Event(), Event()
    cancelled = asyncio.Event()

    def build(*_, **__):
        entered.set()
        assert release.wait(10)
        return application

    monkeypatch.setattr("ouro.tui.controller.LocalApplication.build", build)
    app = OuroTuiApp(home=tmp_path)
    start = app._controller.start

    async def starting():
        try:
            return await start()
        finally:
            cancelled.set()

    monkeypatch.setattr(app._controller, "start", starting)
    async with app.run_test(size=(80, 24)):
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            app.post_message(events.Key(key, None))
            # Input handling awaits the owned constructor, so don't ask Pilot to
            # wait for the event queue to become idle while this gate is held.
            async with asyncio.timeout(5):
                while not app._host_closing:
                    await asyncio.sleep(0.01)
            assert isinstance(app.screen, StartupScreen)
            assert app._startup_screen._message.render().plain == "Exiting…"
            assert not app._exit
            assert not application._closed
        finally:
            release.set()
        await asyncio.wait_for(cancelled.wait(), 5)
    assert application._closed
    assert app.return_value == 0


def test_recovery_releases_home_and_restarts_once_without_resuming_selection(tmp_path, monkeypatch):
    calls = []

    class Host:
        startup_data_error = UnsupportedDataVersionError("old data")
        tui_theme = None

        def __init__(self, **kwargs):
            calls.append(("host", kwargs["resume_session_id"]))

        def run(self):
            return 1

    class Recovery:
        def __init__(self, **kwargs):
            calls.append(("recovery", kwargs["auto_upgrade"]))

        def run(self):
            with Workspace(tmp_path, check_only=True):
                pass
            return "upgraded" if len(calls) == 2 else None

    @contextmanager
    def guard(**_):
        yield

    monkeypatch.setattr("ouro.tui.startup.tui_terminal_guard", guard)
    monkeypatch.setattr("ouro.tui.app.OuroTuiApp", Host)
    monkeypatch.setattr("ouro.tui.recovery.DataRecoveryApp", Recovery)
    assert run_tui(home=tmp_path, resume_session_id="saved") == 1
    assert calls == [("host", "saved"), ("recovery", True), ("host", None), ("recovery", False)]
