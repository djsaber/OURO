from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Static

from ouro.application.supervisor_models import RuntimeToolCallActivity
from ouro.tui.controller import TuiSystemStatus
from ouro.tui.conversation_history import _reasoning_label_text
from ouro.tui.screens import Choice, ChoiceScreen, grouped_choice_options
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme
from ouro.tui.welcome.components import WelcomeSystem, WelcomeTips
from ouro.tui.widgets import ActivityIndicator, _tool_activity_text


def _assert_separators(text: Text, theme: TuiTheme) -> None:
    offsets = [
        index
        for index, char in enumerate(text.plain)
        if char in {theme.inline_separator_mark, theme.section_separator_mark}
    ]
    assert offsets


@pytest.mark.parametrize("separator", ["•", "·"])
def test_reasoning_and_tool_labels_use_theme_separators(
    separator: str,
) -> None:
    theme = replace(
        DEFAULT_TUI_THEME,
        inline_separator_mark=separator,
    )
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
    assert "+2" in rendered.plain


class _VisualApp(App):
    tui_theme = replace(
        DEFAULT_TUI_THEME,
        inline_separator_mark="·",
        section_separator_mark="|",
        selected_choice_marker="◆",
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
        yield WelcomeTips()


async def test_welcome_status_and_menu_separators_preserve_semantic_fields_on_theme_refresh():
    app = _VisualApp()
    async with app.run_test() as pilot:
        welcome = app.query_one(WelcomeSystem)
        indicator = app.query_one(ActivityIndicator)
        tips = app.query_one(WelcomeTips)
        indicator.set_approval_mode("auto")
        theme = app.tui_theme
        title = tips._title.content
        assert title.plain == f"{theme.selected_choice_marker} TIPS"
        for text in (welcome._plugins.content, welcome._mcp.content, indicator.content):
            _assert_separators(text, theme)
        assert "failed" in welcome._mcp.content.plain
        assert "need attention" in welcome._plugins.content.plain
        assert "auto" in indicator.content.plain
        for activity, label, busy in (
            ("idle", "Ready", False),
            ("completed", "Ready", False),
            ("failed", "Failed", False),
            ("aborted", "Aborted", False),
            ("paused", "Paused", False),
            ("recoverable", "Recovery required", False),
            ("waiting_approval", "Approval required", False),
            ("waiting_model", "Waiting for model", True),
        ):
            indicator.set_activity(activity)
            content = indicator.content
            marker = theme.spinner_frames[indicator._frame] if busy else theme.brand_mark
            assert content.plain.startswith(f" {marker} {label}")
        indicator.set_activity("failed")

        choice = Choice(
            "one",
            "One",
            trailing=f"enabled{theme.inline_separator}plugin",
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

        menu.update_choices((choice,), title=f"PLUGIN{theme.inline_separator}One")
        app.tui_theme = replace(
            theme,
            selected_choice_marker="■",
        )
        menu.refresh_theme()
        tips.refresh_theme()
        indicator.refresh_theme()
        assert indicator.content.plain.startswith(f" {app.tui_theme.brand_mark} Failed")
        title = tips._title.content
        assert title.plain == f"{app.tui_theme.selected_choice_marker} TIPS"
        title = menu.query_one("#choice-title", Static).content
        assert title.plain == f"PLUGIN{theme.inline_separator}One"
        _assert_separators(title, app.tui_theme)
