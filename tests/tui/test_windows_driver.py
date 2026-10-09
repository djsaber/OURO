from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from rich.control import Control
from rich.segment import Segment
from textual import events
from textual._compositor import LayoutUpdate
from textual._xterm_parser import XTermParser
from textual.app import App
from textual.geometry import Offset, Region, Size
from textual.strip import Strip

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows terminal driver")


@pytest.mark.parametrize("mode", [0, 2])
async def test_terminal_negotiates_atomic_frames_and_restores_cursor_before_publishing(
    monkeypatch, mode
):
    from textual.drivers.windows_driver import WindowsDriver

    from ouro.tui.windows_driver import OuroWindowsDriver

    writes = []

    # Exercise the actual adapter and App output path without taking over a console.
    def start(driver):
        size = Size(*driver._size)
        driver.send_message(events.Resize(size, size))

    monkeypatch.setattr(WindowsDriver, "start_application_mode", start)
    monkeypatch.setattr(WindowsDriver, "stop_application_mode", lambda _: None)
    monkeypatch.setattr(WindowsDriver, "write", lambda _, data: writes.append(data))
    app = App(driver_class=OuroWindowsDriver)
    async with app.run_test(headless=False, size=(120, 40)) as pilot:
        assert "\x1b[?2026$p" in writes
        parser = XTermParser()
        for message in parser.feed(f"\x1b[?2026;{mode}$y"):
            app.post_message(message)
        await pilot.pause()
        assert app._sync_available is bool(mode)
        app.cursor_position = Offset(9, 32)
        # A full-color artwork frame crosses Windows' 8192-character write boundary.
        update = LayoutUpdate(
            [[Strip([Segment("\x1b[38;2;70;80;90m⣿" * 1200)])]], Region(0, 0, 120, 1)
        )
        writes.clear()
        app._display(app.screen, update)
        # A frame must reach the writer as one unit, including cursor restoration.
        # Merely wrapping fragmented writes in DEC 2026 does not stabilize the
        # native cursor read by IME clients.
        assert len(writes) == 1
        sequence = "".join(writes)
        restore = Control.move_to(9, 32).segment.text
        if mode:
            assert sequence.startswith("\x1b[?2026h")
            assert sequence.endswith(restore + "\x1b[?2026l")
        else:
            assert "\x1b[?2026h" not in sequence
            assert sequence.endswith(restore)


async def test_driver_drains_standalone_controls_and_pending_output_on_close(monkeypatch):
    from textual.drivers.windows_driver import WindowsDriver

    from ouro.tui.windows_driver import OuroWindowsDriver

    writes = []
    monkeypatch.setattr(WindowsDriver, "write", lambda _, data: writes.append(data))
    monkeypatch.setattr(WindowsDriver, "close", lambda _: writes.append("closed"))
    driver = OuroWindowsDriver(App())
    driver.write("\a")
    await asyncio.sleep(0)
    assert writes == ["\a"]
    driver.write("\x1b[?1049l")
    driver.write("\x1b[?25h")
    driver.close()
    await asyncio.sleep(0)
    assert writes == ["\a", "\x1b[?1049l\x1b[?25h", "closed"]


def test_redirected_console_output_preserves_the_borrowed_stream(tmp_path):
    from ouro.tui.windows_console import ConsoleOutput

    path = tmp_path / "terminal.txt"
    with path.open("w", encoding="utf-8") as stream:
        output = ConsoleOutput(stream)
        text = "汉字🙂⣿" * 10000
        assert output.write(text) == len(text)
        output.flush()
        assert not stream.closed
        assert output.fileno() == stream.fileno()
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("succeeds", [False, True])
def test_failed_or_incomplete_native_frame_is_reported(monkeypatch, tmp_path, succeeds):
    import ctypes

    from ouro.tui import windows_console

    monkeypatch.setattr(windows_console, "_GET_CONSOLE_MODE", lambda *_: True)

    def native_write(*_):
        ctypes.set_last_error(5)
        # A successful call with zero characters written is still a failed frame.
        return succeeds

    monkeypatch.setattr(windows_console, "_WRITE_CONSOLE", native_write)
    with (tmp_path / "output").open("w") as stream:
        output = windows_console.ConsoleOutput(stream)
        assert output.write("") == 0
        with pytest.raises(OSError):
            output.write("汉字🙂⣿")


def test_native_console_frame_keeps_input_cursor_and_unicode_intact():
    # A separate hidden console isolates native API testing from the user's TUI.
    result = subprocess.run(
        [sys.executable, "-m", "tests.tui.windows_console_probe"],
        cwd=Path(__file__).resolve().parents[2],
        creationflags=subprocess.CREATE_NO_WINDOW,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    measured = json.loads(result.stdout)
    assert measured["fragmented_positions"] > 1
    assert measured["native_writes"] == 100
    assert measured["sample_count"] > 0
    assert measured["positions"] == [[9, 25]]
