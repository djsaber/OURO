from __future__ import annotations

from textual import events
from textual.widgets import Input, TextArea


def _is_alt_modified_text(event: events.Key) -> bool:
    """Return whether a printable key is modified by Alt."""

    return event.is_printable and "alt" in event.key.split("+")


class TuiInput(Input):
    """Single-line input that reserves Alt-modified keys for TUI commands."""

    async def _on_key(self, event: events.Key) -> None:
        if _is_alt_modified_text(event):
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)


class TuiTextArea(TextArea):
    """Multi-line input that reserves Alt-modified keys for TUI commands."""

    async def _on_key(self, event: events.Key) -> None:
        if _is_alt_modified_text(event):
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)
