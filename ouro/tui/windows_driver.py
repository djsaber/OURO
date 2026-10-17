"""Windows terminal adapter for complete output frames and native key modifiers."""

from __future__ import annotations

import ctypes

from textual import events
from textual.app import App
from textual.drivers.windows_driver import WindowsDriver
from textual.message import Message

from ouro.tui.keyboard import normalize_windows_alt_key
from ouro.tui.windows_console import ConsoleOutput

_VK_MENU = 0x12
_KEY_DOWN = 0x8000
_USER32 = ctypes.WinDLL("user32", use_last_error=True)
_GET_ASYNC_KEY_STATE = _USER32.GetAsyncKeyState
_GET_ASYNC_KEY_STATE.argtypes = [ctypes.c_int]
_GET_ASYNC_KEY_STATE.restype = ctypes.c_short


def _alt_pressed() -> bool:
    return bool(_GET_ASYNC_KEY_STATE(_VK_MENU) & _KEY_DOWN)


class OuroWindowsDriver(WindowsDriver):
    """Keep frame boundaries and key modifiers intact at the Windows console boundary."""

    def __init__(
        self,
        app: App,
        *,
        debug: bool = False,
        mouse: bool = True,
        size: tuple[int, int] | None = None,
    ) -> None:
        super().__init__(app, debug=debug, mouse=mouse, size=size)
        assert self._file is not None
        # Textual infers TextIOWrapper here, but its writer only needs the stream
        # methods supplied by ConsoleOutput. Keep this adaptation at the boundary.
        self._file = ConsoleOutput(self._file)  # type: ignore[assignment]
        self._output: list[str] = []

    def write(self, data: str) -> None:
        if not self._output:
            # Textual flushes every display update. Standalone controls such as
            # the bell need to drain even when no display update follows them.
            self._loop.call_soon(self.flush)
        self._output.append(data)

    def flush(self) -> None:
        if self._output:
            frame = "".join(self._output)
            self._output.clear()
            super().write(frame)

    def close(self) -> None:
        self.flush()
        super().close()

    def start_application_mode(self) -> None:
        super().start_application_mode()
        # Negotiate visual synchronization separately from native write boundaries.
        self.write("\x1b[?2026$p")
        self.flush()

    def process_message(self, message: Message) -> None:
        if isinstance(message, events.Key):
            message = normalize_windows_alt_key(
                message,
                alt_pressed=_alt_pressed(),
            )
        super().process_message(message)
