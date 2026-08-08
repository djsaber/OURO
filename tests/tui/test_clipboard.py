from __future__ import annotations

from collections.abc import Callable

import pyperclip
import pytest
from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import Static

from ouro.tui.app import _OuroHostScreen
from ouro.tui.clipboard import (
    ClipboardReadError,
    ClipboardWriteError,
    read_system_clipboard,
    write_system_clipboard,
)
from ouro.tui.widgets import Composer


class ComposerApp(App[None]):
    def __init__(self, clipboard_reader: Callable[[], str]) -> None:
        super().__init__()
        self._clipboard_reader = clipboard_reader

    def compose(self) -> ComposeResult:
        yield Composer(clipboard_reader=self._clipboard_reader)

    def get_default_screen(self) -> Screen:
        return _OuroHostScreen(lambda: None, clipboard_writer=lambda _text: None)


class PageSelectionApp(App[None]):
    def __init__(self, clipboard_writer: Callable[[str], None]) -> None:
        super().__init__()
        self._clipboard_writer = clipboard_writer

    def get_default_screen(self) -> Screen:
        return _OuroHostScreen(
            lambda: None,
            clipboard_writer=self._clipboard_writer,
        )

    def compose(self) -> ComposeResult:
        yield Static("copy this page text", id="page-content")


@pytest.mark.asyncio
async def test_composer_right_click_pastes_system_clipboard_at_cursor() -> None:
    reads = 0

    def read_clipboard() -> str:
        nonlocal reads
        reads += 1
        return "pasted\non two lines"

    app = ComposerApp(read_clipboard)

    async with app.run_test(size=(80, 12)) as pilot:
        composer = app.query_one(Composer)
        composer.load_text("before after")
        composer.move_cursor((0, 7))

        await pilot.click(composer, offset=(2, 0), button=3)
        await pilot.pause()

        assert reads == 1
        assert composer.text == "before pasted\non two linesafter"
        assert composer.cursor_location == (1, 12)
        assert composer.has_focus is True


@pytest.mark.asyncio
async def test_page_selection_right_click_copies_and_clears_highlight() -> None:
    copied: list[str] = []
    app = PageSelectionApp(copied.append)

    async with app.run_test(size=(40, 8), notifications=True) as pilot:
        content = app.query_one("#page-content", Static)
        await pilot.mouse_down(content, offset=(0, 0))
        await pilot.hover(content, offset=(9, 0))
        await pilot.mouse_up(content, offset=(9, 0))
        await pilot.pause()

        selected_text = app.screen.get_selected_text()
        assert selected_text

        await pilot.mouse_down(content, offset=(0, 0), button=3)
        await pilot.pause()

        assert copied == [selected_text]
        assert app.screen.get_selected_text() is None
        toast = app.screen.query_one("#textual-toastrack").query_one("Toast")
        assert "Copied to clipboard" in toast.render().plain


@pytest.mark.asyncio
async def test_page_selection_remains_highlighted_when_copy_fails() -> None:
    def fail_to_copy(_text: str) -> None:
        raise ClipboardWriteError("System clipboard is unavailable")

    app = PageSelectionApp(fail_to_copy)

    async with app.run_test(size=(40, 8)) as pilot:
        content = app.query_one("#page-content", Static)
        await pilot.mouse_down(content, offset=(0, 0))
        await pilot.hover(content, offset=(9, 0))
        await pilot.mouse_up(content, offset=(9, 0))
        await pilot.pause()
        selected_text = app.screen.get_selected_text()
        assert selected_text

        await pilot.mouse_down(content, offset=(0, 0), button=3)
        await pilot.pause()

        assert app.screen.get_selected_text() == selected_text


def test_system_clipboard_errors_are_translated(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_to_paste() -> str:
        raise pyperclip.PyperclipException("no clipboard mechanism")

    def fail_to_copy(_text: str) -> None:
        raise pyperclip.PyperclipException("no clipboard mechanism")

    monkeypatch.setattr(pyperclip, "paste", fail_to_paste)
    monkeypatch.setattr(pyperclip, "copy", fail_to_copy)

    with pytest.raises(ClipboardReadError, match="System clipboard is unavailable"):
        read_system_clipboard()

    with pytest.raises(ClipboardWriteError, match="System clipboard is unavailable"):
        write_system_clipboard("copy me")
