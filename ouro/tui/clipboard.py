from __future__ import annotations

import sys

import pyperclip

from ouro.tui.windows_clipboard import read_file_paths


class ClipboardReadError(RuntimeError):
    """The host system clipboard could not provide pasteable text or file paths."""


class ClipboardWriteError(RuntimeError):
    """The host system clipboard could not accept plain text."""


def read_system_clipboard() -> str:
    """Normalize clipboard text or native file paths into the terminal paste format."""
    try:
        text = pyperclip.paste()
        if text == "" and sys.platform == "win32":
            return "\n".join(f'"{path}"' for path in read_file_paths())
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
