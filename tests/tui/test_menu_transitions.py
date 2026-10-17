from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from rich.console import RenderableType
from rich.text import Text
from textual.app import App
from textual.screen import Screen
from textual.widgets import Input, OptionList, Static

from ouro.tui.app import OuroTuiApp
from ouro.tui.screens import Choice, ChoiceScreen, MultiChoiceScreen
from ouro.tui.theme import DEFAULT_TUI_THEME
from tests.tui.helpers import run_ready_app
from tests.tui.test_app import _application, _wait_for_screen


@pytest.mark.parametrize("close", (False, True))
@pytest.mark.parametrize("filter_active", (False, True))
async def test_waiting_choice_animates_without_blocking_filter_navigation_or_close(
    close, filter_active
):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def select(value):
        calls.append(value)
        entered.set()
        await release.wait()
        return (Choice("slow", "Finished"), Choice("other", "Other"))

    screen = ChoiceScreen(
        title="OPERATIONS",
        placeholder="Filter…",
        choices=(Choice("slow", "Slow operation"), Choice("other", "Other")),
        on_select=select,
    )
    app = App()
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.press("enter")
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            await pilot.pause()
            options = screen.query_one(OptionList)
            prompt = options.get_option("slow").prompt
            assert isinstance(prompt, Text)
            assert prompt.plain.startswith(DEFAULT_TUI_THEME.spinner_frames)
            first_frame = prompt.plain
            await pilot.pause(DEFAULT_TUI_THEME.spinner_interval_seconds * 1.2)
            assert options.get_option("slow").prompt.plain != first_frame
            await pilot.press("enter", "down")
            assert calls == ["slow"]
            assert options.get_option_at_index(options.highlighted).id == "other"
            if filter_active:
                screen.query_one(Input).focus()
                await pilot.press(*"Other")
                assert screen.query_one(Input).value == "Other"
                assert screen.query_one("#choice-hint", Static).content.startswith(
                    DEFAULT_TUI_THEME.spinner_frames
                )
            if close:
                await pilot.press("escape")
                assert app.screen is not screen
        finally:
            release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        if not close:
            assert screen.query_one(Input).value == ("Other" if filter_active else "")
            assert options.get_option_at_index(options.highlighted).id == "other"
            assert screen._spinner_timer is None
            assert not screen.query_one("#choice-hint", Static).content.startswith(
                DEFAULT_TUI_THEME.spinner_frames
            )
        else:
            assert app.screen is not screen


@pytest.mark.parametrize("operation", ("back", "tab"))
async def test_page_refresh_wait_keeps_filter_and_close_responsive(operation):
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(_direction=1):
        entered.set()
        await release.wait()
        return (Choice("row", "Refreshed"),)

    screen = ChoiceScreen(
        title="PARENT",
        placeholder="Filter…",
        choices=(Choice("row", "Original"),),
        on_refresh=refresh,
        on_tab=refresh,
    )
    app = App()
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        if operation == "back":
            screen.push_page(
                title="CHILD", placeholder="Filter…", choices=(Choice("row", "Child"),)
            )
        await pilot.press("escape" if operation == "back" else "tab")
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            assert screen._title == "PARENT"
            assert screen.query_one(OptionList).get_option("row").prompt.plain == "Original"
            assert screen.query_one("#choice-hint", Static).content.startswith(
                DEFAULT_TUI_THEME.spinner_frames
            )
            screen.query_one(Input).focus()
            await pilot.press(*"Original")
            assert screen.query_one(Input).value == "Original"
            await pilot.press("escape")
            assert app.screen is not screen
        finally:
            release.set()
        await app.workers.wait_for_complete()
        assert app.screen is not screen


@pytest.mark.parametrize("close", (False, True))
async def test_pending_multichoice_confirmation_respects_back_and_close(close):
    entered, release = asyncio.Event(), asyncio.Event()

    async def confirm(values):
        entered.set()
        await release.wait()
        return (Choice("remaining", "Remaining"),)

    screen = MultiChoiceScreen(
        title="ITEMS",
        placeholder="Filter…",
        choices=(Choice("item", "Item"), Choice("remaining", "Remaining")),
        on_confirm=confirm,
        selected_marker=DEFAULT_TUI_THEME.selected_choice_marker,
    )
    app = App()
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.press("enter", "down", "enter")
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            await pilot.press("escape")
            screen.query_one(Input).focus()
            await pilot.press(*"Remaining")
            if close:
                await pilot.press("escape")
                assert app.screen is not screen
        finally:
            release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        if not close:
            assert screen.query_one(Input).value == "Remaining"
            assert not screen._confirmation_values


@pytest.mark.parametrize("operation", ("refresh", "push", "replace", "switch", "child"))
async def test_background_navigation_ignores_a_closed_menu(operation):
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh():
        entered.set()
        await release.wait()
        return (Choice("row", "Refreshed"),)

    screen = ChoiceScreen(
        title="MENU",
        placeholder="Filter…",
        choices=(Choice("row", "Original"),),
        on_refresh=refresh,
    )
    app = App()
    async with app.run_test() as pilot:
        await app.push_screen(screen)

        async def navigate():
            if operation == "refresh":
                return await screen.refresh_choices()
            choices = await refresh()
            page = dict(title="LATE", placeholder="Filter…", choices=choices)
            if operation == "child":
                return await screen.open_child(ChoiceScreen(**page))
            if operation == "switch":
                return screen.switch_page(navigation_id="plugins", **page)
            if operation == "push":
                return screen.push_page(**page)
            return screen.replace_page(**page)

        pending = asyncio.create_task(navigate())
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            await pilot.press("escape")
            assert app.screen is not screen
        finally:
            release.set()
        assert not await pending
        assert app.screen is not screen and len(app.screen_stack) == 1


@pytest.mark.parametrize("route", ("shortcut", "action"))
@pytest.mark.parametrize("close", (False, True))
async def test_plugin_catalog_wait_has_feedback_and_respects_menu_close(
    tmp_path, monkeypatch, route, close
):
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    entered, release = asyncio.Event(), asyncio.Event()
    async with run_ready_app(app) as pilot:
        plugins = await app._controller.plugins()

        async def slow_plugins():
            entered.set()
            await release.wait()
            return plugins

        monkeypatch.setattr(app._controller, "plugins", slow_plugins)
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        menu = app.screen
        menu.query_one(Input).value = "Manage plugins"
        await pilot.pause()
        await pilot.press("alt+p" if route == "shortcut" else "enter")
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            assert app.screen is menu and menu._title == "ACTIONS"
            if route == "shortcut":
                assert menu.query_one("#choice-hint", Static).content.startswith(
                    app.tui_theme.spinner_frames
                )
            else:
                assert any(
                    "Manage plugins" in strip.text
                    and any(frame in strip.text for frame in app.tui_theme.spinner_frames)
                    for strip in menu._compositor.render_strips()
                )
            if close:
                await pilot.press("escape")
                assert app.screen is not menu
        finally:
            release.set()
        await app.workers.wait_for_complete(
            [
                worker
                for worker in app.workers
                if worker.group == "navigation" or worker.group.startswith("choice-")
            ]
        )
        await pilot.pause()
        if close:
            assert app.screen is not menu
        else:
            assert app.screen is menu and menu._title == "PLUGINS"
            assert menu._spinner_timer is None


@contextmanager
def _parent_frames(
    app: OuroTuiApp, parent: ChoiceScreen, monkeypatch: pytest.MonkeyPatch
) -> Iterator[list[str]]:
    """Observe actual repaint boundaries, including frames before Pilot settles."""

    frames: list[str] = []
    display = app._display

    def capture(screen: Screen, renderable: RenderableType | None) -> None:
        if screen is parent and renderable is not None and not app._batch_count:
            frames.append("\n".join(strip.text for strip in screen._compositor.render_strips()))
        display(screen, renderable)

    with monkeypatch.context() as patch:
        patch.setattr(app, "_display", capture)
        yield frames


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "title"),
    [
        ("Context compaction", "CONTEXT COMPACTION"),
        ("Rename session", "RENAME SESSION • USER"),
        ("Delete session", "DELETE SESSIONS • ARCHIVED"),
        ("+ task", "CREATE TASK"),
        ("Install plugin", "INSTALL PLUGIN"),
        ("Tool limits & file access", "TOOL LIMITS & FILE ACCESS"),
        ("Subagent permissions", "SUBAGENT PERMISSIONS"),
        ("Create subagent", "CREATE SUBAGENT"),
        ("+ mcp", "ADD MCP"),
        ("Install skill", "INSTALL SKILL"),
    ],
)
async def test_child_return_renders_complete_parent_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, title: str
) -> None:
    application = _application(tmp_path)
    application.runtime.sessions.create(session_id="ses_active", title="Active session")
    archived = application.runtime.sessions.create(
        session_id="ses_archived", title="Archived session"
    ).session
    application.runtime.sessions.set_archived(archived.session_id, True)
    app = OuroTuiApp(home=tmp_path, application=application)
    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        parent = app.screen
        assert isinstance(parent, ChoiceScreen)
        filter_input = parent.query_one("#choice-filter", Input)
        filter_input.value = action
        filter_input.cursor_position = len(action)
        await pilot.pause()
        selected = parent.query_one(OptionList).highlighted
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title=title)
        await pilot.pause()
        assert app.screen is not parent

        with _parent_frames(app, parent, monkeypatch) as frames:
            await pilot.press("escape")
            await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
            await pilot.pause()
        assert app.screen is parent
        assert frames
        assert all("ACTIONS" in frame and action in frame for frame in frames)
        assert filter_input.value == action
        assert filter_input.has_focus
        assert filter_input.selection.is_empty
        assert filter_input.cursor_position == len(action)
        assert app.cursor_position == filter_input.cursor_screen_offset
        assert parent.query_one(OptionList).highlighted == selected
        assert parent.styles.background.a == 0.65
        assert len(app.screen_stack) == 2
        await pilot.press("x")
        assert filter_input.value == action + "x"


@pytest.mark.asyncio
@pytest.mark.parametrize("accept", [False, True])
async def test_nested_child_return_after_resize_preserves_menu_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accept: bool
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    async with run_ready_app(app, size=(100, 42)) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        actions = app.screen
        actions.query_one(Input).value = "Context compaction"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="CONTEXT COMPACTION")
        parent = app.screen
        assert isinstance(parent, ChoiceScreen)
        options = parent.query_one(OptionList)
        options.highlighted = options.get_option_index("compaction-model")
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="COMPACTION MODEL")
        await pilot.resize_terminal(90, 30)
        await pilot.pause()

        with _parent_frames(app, parent, monkeypatch) as frames:
            await pilot.press("enter" if accept else "escape")
            await _wait_for_screen(app, ChoiceScreen, title="CONTEXT COMPACTION")
            await pilot.pause()
        assert frames
        assert all("CONTEXT COMPACTION" in frame for frame in frames)
        assert app.screen is parent
        assert options.has_focus if accept else parent.query_one(Input).has_focus
        assert options.get_option_at_index(options.highlighted).id == "compaction-model"
        assert parent.styles.background.a == 0.65
        assert actions.styles.background.a == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_for_result", (False, True))
async def test_child_open_and_return_preserve_scrolled_parent_without_empty_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait_for_result: bool
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        parent = app.screen
        assert isinstance(parent, ChoiceScreen)
        options = parent.query_one(OptionList)
        options.highlighted = options.option_count - 1
        await pilot.pause()
        selected, scroll_y = options.highlighted, options.scroll_y
        assert scroll_y > 0
        entered, release = asyncio.Event(), asyncio.Event()

        async def choices():
            entered.set()
            await release.wait()
            return (Choice("ok", "OK"),)

        child = ChoiceScreen(title="CHILD", placeholder="Filter…", choices=(), on_refresh=choices)
        with (
            _parent_frames(app, parent, monkeypatch) as frames,
            _parent_frames(app, child, monkeypatch) as child_frames,
        ):
            pending = asyncio.create_task(parent.open_child(child)) if wait_for_result else None
            if pending is None:
                parent.show_child(child)
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                parent.refresh()
                await pilot.pause()
                assert app.screen is parent
                assert frames
                assert all("ACTIONS" in frame and "Keyboard help" in frame for frame in frames)
                assert options.highlighted == selected and options.scroll_y == scroll_y
            finally:
                release.set()
            await _wait_for_screen(app, ChoiceScreen, title="CHILD")
            await pilot.pause()
        assert child_frames and all("CHILD" in frame and "OK" in frame for frame in child_frames)
        assert child.query_one(OptionList).option_count == 1
        with _parent_frames(app, parent, monkeypatch) as frames:
            await pilot.press("escape")
            await pilot.pause()
        if pending is not None:
            assert await pending is None
        assert frames
        assert all("ACTIONS" in frame and "Keyboard help" in frame for frame in frames)
        assert options.highlighted == selected
        assert options.scroll_y == scroll_y
        assert parent.query_one(Input).has_focus
