"""Windows terminal adapter preserving modifiers omitted from VT key events."""

from __future__ import annotations

import ctypes

from textual import events
from textual.drivers.windows_driver import WindowsDriver
from textual.message import Message

from ouro.tui.keyboard import (
    normalize_windows_alt_key,
    normalize_windows_ctrl_h,
    normalize_windows_modified_enter,
)

_VK_CONTROL = 0x11
_VK_SHIFT = 0x10
_VK_MENU = 0x12
_KEY_DOWN = 0x8000
_USER32 = ctypes.WinDLL("user32", use_last_error=True)
_GET_ASYNC_KEY_STATE = _USER32.GetAsyncKeyState
_GET_ASYNC_KEY_STATE.argtypes = [ctypes.c_int]
_GET_ASYNC_KEY_STATE.restype = ctypes.c_short


def _shift_pressed() -> bool:
    return bool(_GET_ASYNC_KEY_STATE(_VK_SHIFT) & _KEY_DOWN)


def _ctrl_pressed() -> bool:
    return bool(_GET_ASYNC_KEY_STATE(_VK_CONTROL) & _KEY_DOWN)


def _alt_pressed() -> bool:
    return bool(_GET_ASYNC_KEY_STATE(_VK_MENU) & _KEY_DOWN)


class OuroWindowsDriver(WindowsDriver):
    """Textual Windows driver preserving modifiers omitted from VT key events."""

    def process_message(self, message: Message) -> None:
        if isinstance(message, events.Key):
            message = normalize_windows_ctrl_h(
                message,
                ctrl_pressed=_ctrl_pressed(),
            )
            if message.key == "enter":
                message = normalize_windows_modified_enter(
                    message,
                    shift_pressed=_shift_pressed(),
                )
            message = normalize_windows_alt_key(
                message,
                alt_pressed=_alt_pressed(),
            )
        super().process_message(message)
