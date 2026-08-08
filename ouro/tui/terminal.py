"""Terminal-emulator presentation controls for the TUI host."""

from __future__ import annotations

import sys
from typing import TextIO

_TITLE_ESCAPE = "\x1b]0;{title}\x07"


def set_terminal_title(title: str, *, stream: TextIO | None = None) -> bool:
    """Set the terminal tab title without making TUI startup depend on it."""

    output = stream if stream is not None else sys.stdout
    try:
        if not output.isatty():
            return False
        safe_title = "".join(character for character in title if character.isprintable())
        output.write(_TITLE_ESCAPE.format(title=safe_title))
        output.flush()
    except (OSError, ValueError):
        return False
    return True


__all__ = ["set_terminal_title"]
