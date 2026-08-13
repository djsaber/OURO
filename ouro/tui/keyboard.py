"""Terminal key normalization independent from the Textual driver lifecycle."""

from __future__ import annotations

from textual.events import Key


def normalize_windows_ctrl_h(event: Key, *, ctrl_pressed: bool) -> Key:
    """Restore Ctrl+H when Textual parses its shared byte as Backspace."""

    if event.key == "backspace" and ctrl_pressed:
        return Key("ctrl+h", None)
    return event


def normalize_windows_alt_key(event: Key, *, alt_pressed: bool) -> Key:
    """Restore Alt when the Windows console reports only the produced character."""

    if not alt_pressed or "alt" in event.key.split("+"):
        return event
    return Key(f"alt+{event.key}", event.character)


def normalize_windows_modified_enter(event: Key, *, shift_pressed: bool) -> Key:
    """Restore Shift+Enter when the Windows console reports a plain Enter event."""

    if event.key == "enter" and shift_pressed:
        return Key("shift+enter", None)
    return event
