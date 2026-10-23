"""TUI application scenarios: navigation."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouro.core.messages import MessageRecord, TextContent
from ouro.tui.app import OuroTuiApp
from ouro.tui.commands import TUI_COMMANDS
from ouro.tui.composer.input import Composer
from ouro.tui.composer.popup import CommandPopup
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.conversation.view import ConversationView
from ouro.tui.menus.choice import ChoiceScreen
from tests.tui.app.support import _application
from tests.tui.helpers import command_popup_text, run_ready_app, wait_for_history_render
from tests.tui.helpers import wait_for_screen as _wait_for_screen


@pytest.mark.asyncio
async def test_stream_updates_follow_only_while_conversation_is_at_bottom(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 24)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        for index in range(40):
            await conversation.add_notice(f"History {index}")
        await pilot.pause()

        assert conversation.is_vertical_scroll_end is True

        conversation.scroll_to(y=0, animate=False, immediate=True)
        await pilot.pause()
        held_position = conversation.scroll_y
        await conversation.add_notice("New streamed content while reviewing history")
        await pilot.pause()

        assert conversation.scroll_y == held_position
        assert conversation.is_vertical_scroll_end is False

        conversation.scroll_to(
            y=conversation.max_scroll_y,
            animate=False,
            immediate=True,
        )
        await pilot.pause()
        await conversation.add_notice("New streamed content after returning to bottom")
        await pilot.pause()

        assert conversation.is_vertical_scroll_end is True


@pytest.mark.asyncio
async def test_short_conversation_starts_at_top_while_following_latest_message(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.add_user("First question")
        await conversation.append_assistant("First answer")
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        assert conversation.scroll_y == 0
        assert history.user_lines == (0,)
        assert history.region.y <= conversation.content_region.y + 2
        assert conversation.is_vertical_scroll_end is True


@pytest.mark.asyncio
async def test_navigate_command_in_catalog() -> None:
    """The /navigate command is registered in the command catalog."""
    assert any(cmd.name == "/navigate" for cmd in TUI_COMMANDS)


def _scrollable_messages() -> tuple[MessageRecord, ...]:
    """Build a transcript tall enough to scroll inside a small terminal."""
    return tuple(
        MessageRecord(
            session_id="ses_nav",
            role=role,
            content=[TextContent(text=text)],
        )
        for i in range(15)
        for role, text in (
            ("user", f"Question {i}"),
            ("assistant", f"Answer {i} with enough detail to occupy a line"),
        )
    )


@pytest.mark.asyncio
async def test_navigate_to_top_scrolls_to_first_message(tmp_path: Path) -> None:
    """Selecting Top jumps the conversation to the first message."""
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(80, 12)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(_scrollable_messages())
        await wait_for_history_render(pilot)
        await pilot.pause()
        assert conversation.scroll_y > 0  # anchored at the bottom after load

        await app._dispatch_command("/navigate top")
        await pilot.pause()
        assert conversation.scroll_y == 0


@pytest.mark.asyncio
async def test_navigate_to_bottom_scrolls_to_latest(tmp_path: Path) -> None:
    """Selecting Bottom re-anchors the conversation to the latest message."""
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(80, 12)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(_scrollable_messages())
        await wait_for_history_render(pilot)
        await pilot.pause()

        conversation.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        assert conversation.scroll_y == 0

        await app._dispatch_command("/navigate bottom")
        await pilot.pause()
        assert conversation.scroll_y == conversation.max_scroll_y


@pytest.mark.asyncio
async def test_conversation_shortcuts_navigate_adjacent_user_messages(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(80, 12)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(_scrollable_messages())
        await wait_for_history_render(pilot)
        history = conversation.query_one(ConversationHistory)
        composer = app.query_one(Composer)
        composer.load_text("draft")
        composer.focus()

        conversation.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        await pilot.press("alt+pageup")
        await pilot.pause()
        assert conversation.scroll_y == history.user_lines[0]

        await pilot.press("alt+pagedown")
        await pilot.pause()
        assert conversation.scroll_y == history.user_lines[1]

        await pilot.press("alt+pagedown")
        await pilot.pause()
        assert conversation.scroll_y == history.user_lines[2]

        await pilot.press("alt+pageup")
        await pilot.pause()
        assert conversation.scroll_y == history.user_lines[1]
        assert composer.text == "draft"

        conversation.scroll_to_user_turn(len(history.user_lines) - 1)
        await pilot.pause()
        await pilot.press("alt+pagedown")
        await pilot.pause()
        assert conversation.scroll_y == conversation.max_scroll_y

        await pilot.press("alt+pagedown")
        await pilot.pause()
        assert conversation.scroll_y == conversation.max_scroll_y


@pytest.mark.asyncio
async def test_navigate_to_user_message_scrolls_to_target(tmp_path: Path) -> None:
    """Picking the first user message scrolls it to the top of the viewport."""
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(80, 12)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(_scrollable_messages())
        await wait_for_history_render(pilot)
        await pilot.pause()
        assert conversation.scroll_y > 0  # at the bottom

        await app._dispatch_command("/navigate #1")
        await pilot.pause()

        history = conversation.query_one(ConversationHistory)
        assert history.user_lines[0] == 0
        assert conversation.scroll_y == history.user_lines[0]


@pytest.mark.asyncio
async def test_navigate_options_list_user_messages(tmp_path: Path) -> None:
    """The /navigate argument options include top, bottom, and every user message."""
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(80, 12)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        await conversation.load_messages(_scrollable_messages())
        await pilot.pause()

        options = app._navigate_options()
        values = [opt.value for opt in options]
        assert values[:2] == ["top", "bottom"]
        # 15 user messages → #1 through #15
        assert values[2:] == [f"#{i}" for i in range(1, 16)]


@pytest.mark.asyncio
async def test_plugin_shortcut_navigates_beneath_actions_without_stacking(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        menu = app.screen

        for key, title in (
            ("alt+p", "PLUGINS"),
            ("alt+p", "PLUGINS"),
        ):
            await pilot.press(key)
            await _wait_for_screen(app, ChoiceScreen, title=title)
            assert app.screen is menu
            assert len(menu._page_stack) == 1
            assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert app.screen is menu


@pytest.mark.asyncio
async def test_slash_opens_filters_navigates_and_executes_command_popup(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("slash")
        await pilot.pause()

        popup = app.query_one(CommandPopup)
        assert popup.display is True
        popup_content = popup._content
        assert popup_content.content_region.width == popup.scrollable_content_region.width
        assert "abort" in app.export_screenshot()
        popup_text = command_popup_text(popup).plain
        assert "/config" not in popup_text
        assert "/tools" not in popup_text
        assert "/mcp" not in popup_text
        assert "/skills" not in popup_text
        assert popup.selected_command is not None
        assert popup.selected_command.name == "/abort"

        await pilot.press("down")
        await pilot.pause()
        assert popup.selected_command is not None
        assert popup.selected_command.name == "/actions"

        await pilot.press("down")
        await pilot.pause()
        assert popup.selected_command is not None
        assert popup.selected_command.name == "/approval"
        assert popup.scroll_y == 0

        for _ in range(20):
            if popup.selected_command is not None and popup.selected_command.name == "/summary":
                break
            await pilot.press("down")
        await pilot.pause()
        assert popup.selected_command is not None
        assert popup.selected_command.name == "/summary"

        await pilot.press("escape")
        assert popup.display is False
        assert app.query_one(Composer).text == "/"

        app.query_one(Composer).clear()
        await pilot.press("slash", "h")
        await pilot.pause()
        assert popup.display is True
        assert popup.selected_command is not None
        assert popup.selected_command.name == "/help"

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        assert app.screen.navigation_id == "help"
