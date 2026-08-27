from __future__ import annotations

from collections.abc import Callable

import pyperclip
import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.geometry import Offset
from textual.screen import Screen
from textual.selection import Selection
from textual.widgets import Static

from ouro.application.agent_models import AgentTreeNode
from ouro.application.supervisor_models import RuntimeToolCallActivity
from ouro.tui.app import _OuroHostScreen
from ouro.tui.clipboard import (
    ClipboardReadError,
    ClipboardWriteError,
    read_system_clipboard,
    write_system_clipboard,
)
from ouro.tui.conversation_history import (
    AssistantTailLines,
    ConversationHistory,
    TranscriptEntry,
)
from ouro.tui.theme import TuiTheme
from ouro.tui.widgets import Composer, ConversationView


def _unused_tool_renderer(
    tool: RuntimeToolCallActivity,
    *,
    frame: int,
    theme: TuiTheme,
    agent: AgentTreeNode | None = None,
) -> Text:
    del tool, frame, theme, agent
    return Text()


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


class HistorySelectionApp(PageSelectionApp):
    CSS = "ConversationHistory { width: 100%; height: 4; }"

    def compose(self) -> ComposeResult:
        yield ConversationHistory(_unused_tool_renderer)


class ConversationSelectionApp(PageSelectionApp):
    CSS = """
    ConversationView { width: 100%; height: 8; }
    #conversation-transcript { width: 100%; height: auto; }
    ConversationHistory, AssistantTailLines { width: 100%; height: auto; }
    """

    def compose(self) -> ComposeResult:
        yield ConversationView()


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
async def test_page_selection_right_click_copies_and_confirms_external_result() -> None:
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


@pytest.mark.asyncio
async def test_history_right_click_copies_only_the_dragged_text() -> None:
    copied: list[str] = []
    app = HistorySelectionApp(copied.append)

    async with app.run_test(size=(40, 8)) as pilot:
        history = app.query_one(ConversationHistory)
        history.replace((TranscriptEntry(kind="assistant", text="copy only this text"),))
        await history.wait_until_rendered()
        await pilot.pause()

        await pilot.click(history, offset=(2, 0), button=3)
        await pilot.pause()
        assert copied == []

        await pilot.mouse_down(history, offset=(7, 0))
        await pilot.hover(history, offset=(10, 0))
        await pilot.mouse_up(history, offset=(10, 0))
        await pilot.pause()

        assert app.screen.get_selected_text() == "only"
        assert history.text_selection == Selection(Offset(7, 0), Offset(11, 0))
        selection_background = app.screen.get_component_rich_style(
            "screen--selection", partial=True
        ).bgcolor
        selected_segment = next(
            segment for segment in history.render_line(0) if segment.text == "only"
        )
        assert selected_segment.style is not None
        assert selected_segment.style.bgcolor == selection_background
        assert selected_segment.style.color != selection_background

        await pilot.mouse_down(history, offset=(7, 0), button=3)
        await pilot.pause()
        assert copied == ["only"]
        assert app.screen.get_selected_text() is None


@pytest.mark.parametrize("clicks", (2, 3))
@pytest.mark.asyncio
async def test_conversation_chained_clicks_never_select_complete_messages(clicks: int) -> None:
    app = ConversationSelectionApp(lambda _text: None)

    async with app.run_test(size=(50, 10)) as pilot:
        conversation = app.query_one(ConversationView)
        await conversation.add_user("historical message must remain range-selectable only")
        history = app.query_one(ConversationHistory)

        await pilot.click(history, offset=(4, 0), times=clicks)
        await pilot.pause()

        assert app.screen.get_selected_text() is None

        await conversation.append_assistant("live response must also reject select-all")
        tail = app.query_one(AssistantTailLines)
        await pilot.click(tail, offset=(4, 0), times=clicks)
        await pilot.pause()

        assert app.screen.get_selected_text() is None


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
