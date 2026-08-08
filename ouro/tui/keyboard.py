"""Terminal key normalization independent from the Textual driver lifecycle."""

from __future__ import annotations

from textual.events import Key


def normalize_windows_modified_enter(event: Key, *, shift_pressed: bool) -> Key:
    """Restore Shift+Enter when the Windows console reports a plain Enter event."""

    if event.key == "enter" and shift_pressed:
        return Key("shift+enter", None)
    return event
