from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from rich.console import Console
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Static

from ouro.application.supervisor_models import RuntimeToolCallActivity
from ouro.tui.controller import TuiSystemStatus
from ouro.tui.conversation_history import _reasoning_label_text
from ouro.tui.screens import Choice, ChoiceScreen, grouped_choice_options
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme
from ouro.tui.welcome.components import WelcomeSystem
from ouro.tui.widgets import ActivityIndicator, _tool_activity_text


def _assert_separators(text: Text, theme: TuiTheme) -> None:
    offsets = [
        index
        for index, char in enumerate(text.plain)
        if char in {theme.inline_separator_mark, theme.section_separator_mark}
    ]
    assert offsets
    for offset in offsets:
        style = text.get_style_at_offset(Console(), offset)
        assert style.color is not None
        assert style.color.triplet.hex == theme.text_lowlight
        assert not style.dim


@pytest.mark.parametrize("saturation", [0, 31])
@pytest.mark.parametrize("separator", ["•", "·"])
def test_reasoning_and_tool_separators_share_lowlight_without_losing_diff_colors(
    saturation: int, separator: str
) -> None:
    theme = replace(
        DEFAULT_TUI_THEME,
        text_lowlight="#789abc",
        inline_separator_mark=separator,
    ).at_saturation_level(saturation)
    started_at = datetime(2026, 10, 19, tzinfo=UTC)
    tool = RuntimeToolCallActivity(
        tool_call_id="write",
        tool_name="Write",
        source_index=0,
        status="completed",
        arguments={"path": "notes.md", "content": "one\ntwo\n", "mode": "append"},
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=0.5),
    )
    reasoning = _reasoning_label_text("Thought", 0.5, expanded=False, theme=theme)
    rendered = _tool_activity_text(tool, frame=0, theme=theme)
    assert rendered.plain.startswith(f"Write{theme.inline_separator}500ms")
    for text in (reasoning, rendered):
        _assert_separators(text, theme)
    added = rendered.get_style_at_offset(Console(), rendered.plain.index("+2"))
    assert added.color is not None
    assert added.color.triplet.hex == theme.success


class _VisualApp(App):
    tui_theme = replace(
        DEFAULT_TUI_THEME,
        text_lowlight="#789abc",
        inline_separator_mark="·",
        section_separator_mark="|",
    )

    def get_css_variables(self) -> dict[str, str]:
        return {**super().get_css_variables(), **self.tui_theme.css_variables()}

    def compose(self) -> ComposeResult:
        yield WelcomeSystem(
            TuiSystemStatus(
                plugin_active_count=1,
                plugin_total_count=2,
                plugin_issue_count=1,
                mcp_total_count=3,
                mcp_connected_count=1,
                mcp_connecting_count=1,
                mcp_failed_count=1,
            )
        )
        yield ActivityIndicator()


async def test_welcome_status_and_menu_separators_preserve_semantic_fields_on_theme_refresh():
    app = _VisualApp()
    async with app.run_test() as pilot:
        welcome = app.query_one(WelcomeSystem)
        indicator = app.query_one(ActivityIndicator)
        indicator.set_approval_mode("auto")
        theme = app.tui_theme
        for text in (welcome._plugins.content, welcome._mcp.content, indicator.content):
            _assert_separators(text, theme)
        failure = welcome._mcp.content
        assert failure.get_style_at_offset(app.console, failure.plain.index("failed")).color == (
            app.console.get_style(theme.critical).color
        )
        status = indicator.content
        assert status.get_style_at_offset(app.console, status.plain.index("auto")).color == (
            app.console.get_style(theme.danger).color
        )

        choice = Choice(
            "one",
            "One",
            trailing=f"enabled{theme.inline_separator}plugin",
            trailing_accent=theme.accent,
        )
        menu = ChoiceScreen(
            title=f"SESSION{theme.section_separator}USER", placeholder="Filter", choices=(choice,)
        )
        await app.push_screen(menu)
        await pilot.pause()
        _assert_separators(menu.query_one("#choice-title", Static).content, theme)
        options = grouped_choice_options((choice,), theme=theme)
        segments = list(app.console.render(options[0].prompt))
        separator_segments = [s for s in segments if theme.inline_separator_mark in s.text]
        assert separator_segments
        assert all(
            s.style.color == app.console.get_style(theme.text_lowlight).color
            for s in separator_segments
        )

        menu.update_choices((choice,), title=f"PLUGIN{theme.inline_separator}One")
        app.tui_theme = replace(theme, text_lowlight="#abcdef")
        menu.refresh_theme()
        title = menu.query_one("#choice-title", Static).content
        assert title.plain == f"PLUGIN{theme.inline_separator}One"
        _assert_separators(title, app.tui_theme)
