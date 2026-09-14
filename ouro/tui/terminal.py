"""Terminal-emulator presentation controls for the TUI host."""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from typing import TextIO

_TITLE_ESCAPE = "\x1b]0;{title}\x07"
_TUI_RESET_ESCAPE = (
    "\x1b[?1000l"  # Mouse button tracking.
    "\x1b[?1002l"  # Mouse drag tracking.
    "\x1b[?1003l"  # All mouse motion tracking.
    "\x1b[?1015l"  # Legacy extended mouse coordinates.
    "\x1b[?1006l"  # SGR mouse coordinates.
    "\x1b[?2004l"  # Bracketed paste.
    "\x1b[?1004l"  # Focus reporting.
    "\x1b[<u"  # Kitty keyboard protocol.
    "\x1b[?1049l"  # Alternate screen.
    "\x1b[?25h"  # Cursor visibility.
)
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


@dataclass(frozen=True)
class _WindowsConsoleState:
    input_handle: int | None
    input_mode: int | None
    output_handle: int | None
    output_mode: int | None


def set_terminal_title(title: str, *, stream: TextIO | None = None) -> bool:
    """Set the terminal tab title without making TUI startup depend on it."""

    output = stream if stream is not None else sys.stdout
    try:
        if not output.isatty():
            return False
        safe_title = "".join(character for character in title if character.isprintable())
        output.write(_TITLE_ESCAPE.format(title=safe_title))
        output.flush()
    except OSError, ValueError:
        return False
    return True


@contextmanager
def tui_terminal_guard(
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> Iterator[None]:
    """Restore terminal modes if the TUI exits through an exceptional path.

    Textual owns the terminal while it is running and remains responsible for its
    normal lifecycle. This guard is deliberately limited to the Host boundary: it
    records Windows console modes before Textual starts, then performs an idempotent
    protocol reset and restores those modes before returning to the invoking shell.
    """

    terminal_in = input_stream if input_stream is not None else sys.__stdin__
    terminal_out = output_stream if output_stream is not None else sys.__stdout__
    console_state = _capture_windows_console_state(terminal_in, terminal_out)
    try:
        yield
    finally:
        _prepare_windows_console_for_reset(console_state)
        try:
            _write_tui_reset(terminal_out)
        finally:
            _restore_windows_console_state(console_state)


def _write_tui_reset(stream: TextIO | None) -> bool:
    if stream is None:
        return False
    try:
        if not stream.isatty():
            return False
        stream.write(_TUI_RESET_ESCAPE)
        stream.flush()
    except OSError, ValueError:
        return False
    return True


def _capture_windows_console_state(
    input_stream: TextIO | None,
    output_stream: TextIO | None,
) -> _WindowsConsoleState | None:
    if sys.platform != "win32":
        return None
    input_handle, input_mode = _windows_console_handle_and_mode(input_stream)
    output_handle, output_mode = _windows_console_handle_and_mode(output_stream)
    if input_handle is None and output_handle is None:
        return None
    return _WindowsConsoleState(input_handle, input_mode, output_handle, output_mode)


def _windows_console_handle_and_mode(stream: TextIO | None) -> tuple[int | None, int | None]:
    if stream is None:
        return None, None
    try:
        import msvcrt

        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = wintypes.DWORD()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_console_mode = kernel32.GetConsoleMode
        get_console_mode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_console_mode.restype = wintypes.BOOL
        if not get_console_mode(handle, ctypes.byref(mode)):
            return None, None
    except OSError, ValueError:
        return None, None
    return handle, mode.value


def _prepare_windows_console_for_reset(state: _WindowsConsoleState | None) -> None:
    if state is None:
        return
    if state.output_handle is not None and state.output_mode is not None:
        _set_windows_console_mode(
            state.output_handle,
            state.output_mode | _ENABLE_VIRTUAL_TERMINAL_PROCESSING,
        )


def _restore_windows_console_state(state: _WindowsConsoleState | None) -> None:
    if state is None:
        return
    if state.input_handle is not None:
        _flush_windows_console_input(state.input_handle)
        if state.input_mode is not None:
            _set_windows_console_mode(state.input_handle, state.input_mode)
    if state.output_handle is not None and state.output_mode is not None:
        _set_windows_console_mode(state.output_handle, state.output_mode)


def _set_windows_console_mode(handle: int, mode: int) -> bool:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        set_console_mode = kernel32.SetConsoleMode
        set_console_mode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        set_console_mode.restype = wintypes.BOOL
        return bool(set_console_mode(handle, mode))
    except OSError, ValueError:
        return False


def _flush_windows_console_input(handle: int) -> bool:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        flush_console_input = kernel32.FlushConsoleInputBuffer
        flush_console_input.argtypes = [wintypes.HANDLE]
        flush_console_input.restype = wintypes.BOOL
        return bool(flush_console_input(handle))
    except OSError, ValueError:
        return False


__all__ = ["set_terminal_title", "tui_terminal_guard"]
