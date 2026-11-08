from __future__ import annotations

import asyncio
from collections.abc import Callable

from textual import events
from textual.geometry import Size
from textual.messages import Prune
from textual.screen import Screen

from ouro.tui.clipboard import ClipboardWriteError, write_system_clipboard
from ouro.tui.conversation.messages import MessageCopyRequested
from ouro.tui.rendering import TuiCompositor


class _OuroHostScreen(Screen):
    """Own Host-level lifecycle and page-selection interactions."""

    DEFAULT_CSS = """
    _OuroHostScreen > .screen--selection {
        color: transparent;
        background: transparent;
        text-style: reverse;
    }
    """

    def __init__(
        self,
        on_prune: Callable[[], None],
        on_resize: Callable[[Size], None] | None = None,
        *,
        clipboard_writer: Callable[[str], None] = write_system_clipboard,
    ) -> None:
        super().__init__(id="_default")
        self._compositor = TuiCompositor()
        self._on_host_prune = on_prune
        self._on_host_resize = on_resize
        self._clipboard_writer = clipboard_writer

    async def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 3 or not (selected_text := self.get_selected_text()):
            return
        event.prevent_default()
        event.stop()
        if await self._copy_to_clipboard(selected_text):
            self.clear_selection()

    async def on_message_copy_requested(self, message: MessageCopyRequested) -> None:
        message.stop()
        await self._copy_to_clipboard(message.text)

    async def _copy_to_clipboard(self, text: str) -> bool:
        try:
            await asyncio.to_thread(self._clipboard_writer, text)
        except ClipboardWriteError as exc:
            self.notify(str(exc), severity="error", markup=False)
            return False
        self.app.copy_to_clipboard(text)
        self.notify("Copied to clipboard", markup=False)
        return True

    async def on_prune(self, event: Prune) -> None:
        self._on_host_prune()
        await super().on_prune(event)

    def on_resize(self, event: events.Resize) -> None:
        if self._on_host_resize is not None:
            self._on_host_resize(event.size)
