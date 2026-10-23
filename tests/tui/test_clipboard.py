from __future__ import annotations

from collections.abc import Callable

import pyperclip
import pytest
from rich.text import Text
from textual import events
from textual._xterm_parser import XTermParser
from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import Input, Static

from ouro.application.contracts.agents import AgentTreeNode
from ouro.application.contracts.execution import RuntimeToolCallActivity
from ouro.tui.app import OuroTuiApp, _OuroHostScreen
from ouro.tui.clipboard import (
    ClipboardReadError,
    ClipboardWriteError,
    read_system_clipboard,
    write_system_clipboard,
)
from ouro.tui.composer.input import Composer
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.history_models import TranscriptEntry
from ouro.tui.conversation.history_rendering import AssistantTailLines
from ouro.tui.conversation.view import ConversationView
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.theme import TuiTheme
from tests.tui.helpers import run_ready_app, two_route_application


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


@pytest.mark.parametrize("overlay", (False, True))
@pytest.mark.parametrize("focus_before_paste", (False, True))
@pytest.mark.parametrize("existing", (False, True))
async def test_terminal_paste_returns_to_original_editor_after_blur(
    tmp_path, overlay, focus_before_paste, existing
):
    application = two_route_application(tmp_path)
    origin = application.runtime.create_session().session_id if existing else None
    app = OuroTuiApp(application=application, resume_session_id=origin)
    parser = XTermParser()

    def feed(data):
        for offset in range(0, len(data), 1024):
            for event in parser.feed(data[offset : offset + 1024]):
                app.post_message(event)

    async with run_ready_app(app) as pilot:
        composer = app.query_one(Composer)
        composer.load_text("draft")
        composer.move_cursor((0, 5))
        if overlay:
            await app.push_screen(ChoiceScreen(title="Search", placeholder="Filter", choices=()))
        await pilot.pause()
        target = app.screen.query_one(Input) if overlay else composer
        assert app.focused is target
        feed("\x1b[O")
        await pilot.pause()
        assert not app.app_focus and app.focused is None
        if focus_before_paste:
            feed("\x1b[I")
        text = "filter" * 1000 if overlay else " short paste"
        feed(f"\x1b[200~{text}\x1b[201~")
        await pilot.pause()
        if overlay:
            assert target.value == text
            assert composer.text == "draft"
        else:
            assert composer.text == "draft" + text
        assert app.app_focus and app.focused is target
        assert not composer.input_content.attachments
        if not focus_before_paste:
            feed("\x1b[I")
            await pilot.pause()
            assert app.focused is target


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
async def test_composer_right_click_merges_native_paste_event() -> None:
    reads = 0

    def read_clipboard() -> str:
        nonlocal reads
        reads += 1
        return "once"

    app = ComposerApp(read_clipboard)

    async with app.run_test(size=(80, 12)) as pilot:
        composer = app.query_one(Composer)

        await pilot.mouse_down(composer, offset=(0, 0), button=3)
        composer.post_message(events.Paste("once"))
        composer.post_message(events.Paste("once"))
        composer.post_message(events.Click(composer, 0, 0, 0, 0, 3, False, False, False))
        await pilot.pause()

        assert reads == 0
        assert composer.text == "once"

        composer.post_message(events.Paste("once"))
        await pilot.pause()

        assert composer.text == "onceonce"

        await pilot.click(composer, offset=(0, 0), button=3)
        await pilot.pause()

        assert reads == 1
        assert composer.text == "onceonceonce"


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

        y = next(y for y, line in enumerate(history.lines) if "copy only this text" in line.text)
        x = history.lines[y].text.index("only")
        await pilot.mouse_down(history, offset=(x, y))
        await pilot.hover(history, offset=(x + 3, y))
        await pilot.mouse_up(history, offset=(x + 3, y))
        await pilot.pause()

        assert app.screen.get_selected_text() == "only"
        await pilot.mouse_down(history, offset=(x, y), button=3)
        await pilot.pause()
        assert copied == ["only"]
        assert app.screen.get_selected_text() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["user", "scaffold", "command"])
async def test_message_hover_and_right_click_copy_preserve_selection_priority(kind) -> None:
    copied: list[str] = []
    app = HistorySelectionApp(copied.append)
    source = 'copy this exact\n    "literal": "**中文**"\n\nlast line  '

    async with app.run_test(size=(40, 10), notifications=True) as pilot:
        history = app.query_one(ConversationHistory)
        history.replace((TranscriptEntry(kind=kind, text=source, scaffold=kind == "scaffold"),))
        await history.wait_until_rendered()
        await pilot.pause()

        message_y = next(
            y for y, line in enumerate(history.lines) if "copy this exact" in line.text
        )
        message_x = history.lines[message_y].text.index("copy this exact")
        next(
            segment
            for segment in history.render_line(message_y)
            if "copy this exact" in segment.text
        )

        await pilot.hover(history, offset=(message_x, message_y))
        await pilot.pause()

        next(
            segment
            for segment in history.render_line(message_y)
            if "copy this exact" in segment.text
        )

        await pilot.click(history, offset=(message_x, message_y))
        await pilot.pause()
        assert copied == []

        await pilot.click(history, offset=(message_x, message_y), button=3)
        await pilot.pause()

        assert copied == [source]
        toast = app.screen.query_one("#textual-toastrack").query_one("Toast")
        assert "Copied to clipboard" in toast.render().plain

        selection_x = message_x + len("copy this ")
        await pilot.mouse_down(history, offset=(selection_x, message_y))
        await pilot.hover(history, offset=(selection_x + len("exact") - 1, message_y))
        await pilot.mouse_up(history, offset=(selection_x + len("exact") - 1, message_y))
        await pilot.pause()
        assert app.screen.get_selected_text() == "exact"

        await pilot.mouse_down(history, offset=(selection_x, message_y), button=3)
        await pilot.pause()

        assert copied == [source, "exact"]
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
