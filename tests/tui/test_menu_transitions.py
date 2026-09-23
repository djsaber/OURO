from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from rich.console import RenderableType
from textual.screen import Screen
from textual.widgets import Input, OptionList

from ouro.tui.app import OuroTuiApp
from ouro.tui.screens import Choice, ChoiceScreen
from tests.tui.helpers import run_ready_app
from tests.tui.test_app import _application, _wait_for_screen


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
        ("Rename session", "RENAME SESSION"),
        ("Delete session", "DELETE SESSIONS"),
        ("+ task", "CREATE TASK"),
        ("Install plugin", "INSTALL PLUGIN"),
        ("Tool limits & file access", "TOOL LIMITS & FILE ACCESS"),
        ("Subagent permissions", "SUBAGENT PERMISSIONS"),
        ("Create subagent", "CREATE SUBAGENT"),
        ("Add MCP server", "ADD MCP SERVER"),
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
async def test_resultless_child_restores_scrolled_parent_without_empty_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        parent.show_child(
            ChoiceScreen(title="CHILD", placeholder="Filter…", choices=(Choice("ok", "OK"),))
        )
        await _wait_for_screen(app, ChoiceScreen, title="CHILD")
        await pilot.pause()
        with _parent_frames(app, parent, monkeypatch) as frames:
            await pilot.press("escape")
            await pilot.pause()
        assert frames
        assert all("ACTIONS" in frame and "Keyboard help" in frame for frame in frames)
        assert options.highlighted == selected
        assert options.scroll_y == scroll_y
        assert parent.query_one(Input).has_focus
