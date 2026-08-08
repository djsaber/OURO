from __future__ import annotations

import pyperclip


class ClipboardReadError(RuntimeError):
    """The host system clipboard could not provide plain text."""


class ClipboardWriteError(RuntimeError):
    """The host system clipboard could not accept plain text."""


def read_system_clipboard() -> str:
    """Read plain text from the host clipboard through one platform boundary."""
    try:
        text = pyperclip.paste()
    except (OSError, pyperclip.PyperclipException) as exc:
        raise ClipboardReadError("System clipboard is unavailable") from exc
    if not isinstance(text, str):
        raise ClipboardReadError("System clipboard does not contain plain text")
    return text


def write_system_clipboard(text: str) -> None:
    """Write plain text to the host clipboard through one platform boundary."""

    try:
        pyperclip.copy(text)
    except (OSError, pyperclip.PyperclipException) as exc:
        raise ClipboardWriteError("System clipboard is unavailable") from exc
