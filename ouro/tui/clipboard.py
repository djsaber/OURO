from __future__ import annotations

import pyperclip


class ClipboardReadError(RuntimeError):
    """The host system clipboard could not provide plain text."""


def read_system_clipboard() -> str:
    """Read plain text from the host clipboard through one platform boundary."""
    try:
        text = pyperclip.paste()
    except (OSError, pyperclip.PyperclipException) as exc:
        raise ClipboardReadError("System clipboard is unavailable") from exc
    if not isinstance(text, str):
        raise ClipboardReadError("System clipboard does not contain plain text")
    return text
