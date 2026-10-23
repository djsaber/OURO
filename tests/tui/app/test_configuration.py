"""TUI application scenarios: configuration."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Input, OptionList

from ouro.application.configuration.facade import ConfigurationFacade
from ouro.tui.app import OuroTuiApp
from ouro.tui.features.configuration.compaction import CompactionConfigurationScreen
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.editable import EditableChoiceScreen
from ouro.tui.menus.models import Choice
from tests.tui.app.support import _application
from tests.tui.helpers import choose_menu, resize_terminal, run_ready_app
from tests.tui.helpers import wait_for_screen as _wait_for_screen


@pytest.mark.asyncio
async def test_actions_groups_granular_settings_by_function_without_descriptions(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()

        await pilot.press("ctrl+p")
        await pilot.pause()
        action_choices = app.screen._choices
        action_values = {choice.value for choice in action_choices}
        assert "/thinking" not in action_values
        assert "/compact" not in action_values
        assert [choice.label for choice in action_choices] == [
            "Theme",
            "Thinking display",
            "Scaffold messages",
            "Collapse traces",
            "Remember drafts",
            "Session grouping",
            "Queued messages",
            "Context compaction",
            "Personal prompts",
            "Manage data",
            "New session",
            "Open session",
            "Rename session",
            "Archive session",
            "Delete session",
            "Manage tasks",
            "Update status",
            "+ task",
            "Manage providers",
            "Manage endpoints",
            "Set default endpoint",
            "+ provider",
            "+ endpoint",
            "Manage plugins",
            "Install plugin",
            "Delete plugin",
            "Tool permissions",
            "Tool limits & file access",
            "Subagent permissions",
            "Manage subagents",
            "Create subagent",
            "Edit subagent",
            "Delete subagent",
            "Manage MCPs",
            "+ mcp",
            "Skill sources",
            "Manage skills",
            "Install skill",
            "Check skill",
            "Keyboard help",
        ]
        assert [choice.group for choice in action_choices] == [
            *("Settings",) * 10,
            *("Sessions",) * 5,
            *("Tasks",) * 3,
            *("Providers",) * 5,
            *("Plugins",) * 3,
            *("Tools",) * 2,
            *("Subagents",) * 5,
            *("MCPs",) * 2,
            *("Skills",) * 4,
            "Support",
        ]
        assert "Configuration" not in {choice.group for choice in action_choices}
        assert "Recent" not in {choice.group for choice in action_choices}
        assert all(choice.description == "" for choice in action_choices)
        assert {choice.label: choice.trailing for choice in action_choices if choice.trailing} == {
            "Theme": app._tui_theme.label,
            "New session": "Ctrl+N",
            "Open session": "Ctrl+O",
            "Manage tasks": "Ctrl+T",
            "Manage skills": "Ctrl+S",
            "Manage plugins": "Alt+P",
            "Keyboard help": "F1",
        }

        await choose_menu(pilot, "Context compaction", title="CONTEXT COMPACTION")
        assert isinstance(app.screen, CompactionConfigurationScreen)
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_configuration_editor_restores_retained_action_menu(tmp_path: Path) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        action_screen = app.screen
        action_screen.query_one("#choice-filter", Input).value = "+ mcp"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, EditableChoiceScreen, title="ADD MCP")

        assert action_screen in app.screen_stack
        assert action_screen.query_one("#choice-panel", Vertical).display is False
        assert app.screen is not action_screen
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert app.screen is action_screen
        assert action_screen.query_one("#choice-panel", Vertical).display is True


@pytest.mark.asyncio
async def test_action_list_and_help_pages_render_once_on_the_shared_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    configuration.add_provider(
        display_name="Custom",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://example.invalid/v1",
    )
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill.\n---\n",
        encoding="utf-8",
    )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        menu = app.screen
        options = menu.query_one(OptionList)

        for action_label, title in (
            ("Manage providers", "PROVIDERS"),
            ("Manage skills", "SKILLS"),
            ("Keyboard help", "KEYBOARD HELP"),
        ):
            menu.query_one("#choice-filter", Input).value = action_label
            await pilot.pause()
            rendered_titles: list[str] = []
            refresh_options = menu._refresh_options

            def track_refresh(
                *,
                needle: str,
                preferred_value: str | None,
                preferred_position: int | None = None,
                _rendered_titles: list[str] = rendered_titles,
                _refresh_options: Callable[..., None] = refresh_options,
            ) -> None:
                _rendered_titles.append(menu.page_title)
                _refresh_options(
                    needle=needle,
                    preferred_value=preferred_value,
                    preferred_position=preferred_position,
                )

            monkeypatch.setattr(menu, "_refresh_options", track_refresh)
            await pilot.press("enter")
            await _wait_for_screen(app, ChoiceScreen, title=title)
            monkeypatch.setattr(menu, "_refresh_options", refresh_options)

            assert app.screen is menu
            assert menu.query_one(OptionList) is options
            assert rendered_titles == [title]
            assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1

            await pilot.press("escape")
            await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")

        assert [choice.label for choice in menu._choices if choice.group == "Recent"] == [
            "Keyboard help",
            "Manage skills",
            "Manage providers",
        ]
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_configuration_editors_replace_visible_parent_at_every_depth(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 42)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        action_screen = app.screen
        action_screen.query_one("#choice-filter", Input).value = "Context compaction"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, CompactionConfigurationScreen, title="CONTEXT COMPACTION")

        compaction_screen = app.screen
        assert action_screen.query_one("#choice-panel", Vertical).display is False

        compaction_screen.query_one(
            "#choice-list", OptionList
        ).highlighted = compaction_screen.query_one("#choice-list", OptionList).get_option_index(
            "compaction-model"
        )
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="COMPACTION MODEL")

        assert compaction_screen.query_one("#choice-panel", Vertical).display is False

        await pilot.press("escape")
        await _wait_for_screen(app, CompactionConfigurationScreen, title="CONTEXT COMPACTION")
        assert app.screen is compaction_screen
        assert compaction_screen.query_one("#choice-panel", Vertical).display is True

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert app.screen is action_screen
        assert action_screen.query_one("#choice-panel", Vertical).display is True


@pytest.mark.asyncio
async def test_shared_inline_editor_keeps_cursor_in_horizontal_viewport(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    long_value = "begin-" + "思考" * 80 + "-end"

    async def save_value(_field_id: str, _value: str) -> tuple[Choice, ...]:
        return ()

    async with run_ready_app(app, size=(64, 24)) as pilot:
        await pilot.pause()
        screen = EditableChoiceScreen(
            title="INLINE EDIT",
            placeholder="Filter values…",
            choices=(
                Choice(
                    "long-value",
                    "Long value",
                    group="VALUE",
                    edit_value=long_value,
                ),
            ),
            on_save=save_value,
        )
        app.push_screen(screen)
        await pilot.pause()

        options = screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("long-value")
        options.focus()
        await pilot.press("enter")
        await pilot.pause()

        def rendered_prompt() -> Text:
            screen._edit_cursor_visible = True
            screen._render_editor()
            prompt = options.get_option("long-value").prompt
            assert isinstance(prompt, Text)
            assert any("reverse" in str(span.style) for span in prompt.spans)
            editor_width = (
                options.scrollable_content_region.width
                - options.get_component_styles("option-list--option").padding.width
            )
            assert prompt.cell_len <= editor_width
            return prompt

        prompt = rendered_prompt()
        assert prompt.plain.startswith("…")
        assert prompt.plain.endswith(" ")

        screen.action_edit_home()
        prompt = rendered_prompt()
        assert prompt.plain.startswith("begin-")
        assert prompt.plain.endswith("…")

        screen._edit_cursor = len(long_value) // 2
        screen._reset_edit_cursor()
        prompt = rendered_prompt()
        assert prompt.plain.startswith("…")
        assert prompt.plain.endswith("…")

        screen.action_edit_end()
        await resize_terminal(pilot, 42, 18)
        await pilot.pause()
        prompt = rendered_prompt()
        assert prompt.plain.startswith("…")
        assert prompt.plain.endswith(" ")
