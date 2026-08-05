from __future__ import annotations

from collections.abc import Callable

import pyperclip
import pytest
from textual.app import App, ComposeResult

from ouro.tui.clipboard import ClipboardReadError, read_system_clipboard
from ouro.tui.widgets import Composer


class ComposerApp(App[None]):
    def __init__(self, clipboard_reader: Callable[[], str]) -> None:
        super().__init__()
        self._clipboard_reader = clipboard_reader

    def compose(self) -> ComposeResult:
        yield Composer(clipboard_reader=self._clipboard_reader)


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


def test_system_clipboard_errors_are_translated(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_to_paste() -> str:
        raise pyperclip.PyperclipException("no clipboard mechanism")

    monkeypatch.setattr(pyperclip, "paste", fail_to_paste)

    with pytest.raises(ClipboardReadError, match="System clipboard is unavailable"):
        read_system_clipboard()
