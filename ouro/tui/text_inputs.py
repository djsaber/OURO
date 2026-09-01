from __future__ import annotations

from typing import cast

from textual import events
from textual.screen import Screen
from textual.widgets import Input, TextArea


def _is_alt_modified_text(event: events.Key) -> bool:
    """Return whether a printable key is modified by Alt."""

    return event.is_printable and "alt" in event.key.split("+")


class _TerminalCursorSync:
    """Keep the terminal cursor aligned with a focused input after layout."""

    def on_mount(self) -> None:
        text_input = cast(Input | TextArea, self)
        text_input.screen.screen_layout_refresh_signal.subscribe(
            text_input,
            self._sync_terminal_cursor_after_layout,
            immediate=True,
        )

    def on_unmount(self) -> None:
        text_input = cast(Input | TextArea, self)
        text_input.screen.screen_layout_refresh_signal.unsubscribe(text_input)

    def _sync_terminal_cursor_after_layout(self, screen: Screen) -> None:
        text_input = cast(Input | TextArea, self)
        if not text_input.has_focus or not screen.is_active or not text_input.is_on_screen:
            return
        cursor_position = text_input.cursor_screen_offset
        if text_input.app.cursor_position == cursor_position:
            return
        text_input.app.cursor_position = cursor_position
        text_input.refresh()


class TuiInput(_TerminalCursorSync, Input):
    """Single-line input that reserves Alt-modified keys for TUI commands."""

    async def _on_key(self, event: events.Key) -> None:
        if _is_alt_modified_text(event):
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)


class TuiTextArea(_TerminalCursorSync, TextArea):
    """Multi-line input that reserves Alt-modified keys for TUI commands."""

    def watch_selection(self) -> None:
        """Finish cursor scrolling after an edit has updated the document geometry."""

        if (
            self.is_mounted
            and self.cursor_screen_offset not in self.scrollable_content_region
        ):
            self.call_next(self._restore_cursor_visibility)

    def _restore_cursor_visibility(self) -> None:
        if self.is_mounted:
            self.scroll_cursor_visible()

    async def _on_key(self, event: events.Key) -> None:
        if _is_alt_modified_text(event):
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)
