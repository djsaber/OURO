"""TUI application scenarios: menus."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from rich.console import Console
from textual.app import App
from textual.widgets import Input, OptionList, Static

from ouro.application.configuration.facade import ConfigurationFacade
from ouro.core.errors import PersistenceError
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest
from ouro.mcp import MCPServerConfigUpdate
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.features.configuration.compaction import CompactionConfigurationScreen
from ouro.tui.features.configuration.mcp import MCPConfigurationScreen
from ouro.tui.features.configuration.tools import ToolExecutionConfigurationScreen
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.editable import EditableChoiceScreen
from ouro.tui.menus.models import Choice
from ouro.tui.preferences import TuiPreferencesStore
from tests.tui.app.support import _application
from tests.tui.helpers import choose_menu, run_ready_app, wait_for_menu_action
from tests.tui.helpers import wait_for_screen as _wait_for_screen
from tests.tui.helpers import wait_until as _wait_until


def test_choice_screen_projects_group_headers_as_disabled_options() -> None:
    options = ChoiceScreen._options(
        (
            Choice("today-1", "First", group="Today"),
            Choice("today-2", "Second", group="Today"),
            Choice("yesterday", "Third", group="Yesterday"),
        ),
        group_accent="#ffffff",
    )

    assert [option.id for option in options] == [
        None,
        "today-1",
        "today-2",
        None,
        None,
        "yesterday",
    ]
    assert options[0].disabled is True
    assert options[0].prompt.plain == "Today"
    assert options[3].disabled is True
    assert options[3].prompt.plain == " "
    assert options[4].disabled is True
    assert options[4].prompt.plain == "Yesterday"


def test_choice_screen_projects_group_status_on_the_group_header() -> None:
    options = ChoiceScreen._options(
        (
            Choice(
                "endpoint",
                "Endpoint",
                group="Disabled provider",
                group_trailing="unavailable",
                disabled=True,
            ),
        ),
        group_accent="#ffffff",
    )

    console = Console(width=40, record=True)
    console.print(options[0].prompt)
    rendered = console.export_text().splitlines()[0]
    assert rendered.startswith("Disabled provider")
    assert rendered.endswith("unavailable")
    assert ChoiceScreen._is_group_header(options[0]) is True
    assert ChoiceScreen._is_group_header(options[1]) is False


def test_group_shortcuts_are_disabled_for_ungrouped_choice_screens() -> None:
    screen = ChoiceScreen(
        title="MODELS",
        placeholder="Filter models…",
        choices=(Choice("model", "Model"),),
    )

    assert screen.check_action("previous_group", ()) is False
    assert screen.check_action("next_group", ()) is False


@pytest.mark.asyncio
async def test_editable_choice_filter_owns_text_input_outside_inline_edit() -> None:
    app = App()

    async def save_value(_choice_id: str, _value: str) -> tuple[Choice, ...]:
        return ()

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = EditableChoiceScreen(
            title="EDITABLE ITEMS",
            placeholder="Filter items…",
            choices=(Choice("item", "Item", group="ITEMS"),),
            on_save=save_value,
        )
        app.push_screen(screen)
        await pilot.pause()

        filter_input = screen.query_one("#choice-filter", Input)
        assert filter_input.has_focus is True

        await pilot.press(*tuple("abcd"), "home", "delete", "end", "backspace")
        assert filter_input.value == "bc"

        await pilot.press("ctrl+u")
        assert filter_input.value == ""

        await pilot.press("enter")
        await pilot.pause()
        assert screen._editing_value == "item"

        await pilot.press("escape")
        await pilot.pause()
        assert screen._editing_value is None
        assert filter_input.has_focus is True

        await pilot.press(*tuple("Item"))
        assert filter_input.value == "Item"

        await pilot.press("escape")
        await pilot.pause()


def test_choice_focus_position_clamps_to_last_enabled_option() -> None:
    options = OptionList(
        *ChoiceScreen._options(
            (
                Choice("first", "First", group="Current"),
                Choice("disabled", "Disabled", group="Current", disabled=True),
                Choice("last", "Last", group="Archived"),
            )
        )
    )

    highlighted = ChoiceScreen._highlight_index(options, preferred_position=99)

    assert highlighted is not None
    assert options.get_option_at_index(highlighted).id == "last"


@pytest.mark.asyncio
async def test_grouped_choice_filter_hides_empty_groups_and_selects_first_choice() -> None:
    app = App()
    results: list[str | None] = []

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.push_screen(
            ChoiceScreen(
                title="SESSIONS",
                placeholder="Filter sessions…",
                choices=(
                    Choice("today", "Today session", group="Today"),
                    Choice("yesterday", "Yesterday session", group="Archive"),
                ),
            ),
            callback=results.append,
        )
        await pilot.pause()

        options = app.screen.query_one(OptionList)
        assert options.option_count == 5
        assert options.highlighted == 1
        await pilot.press("right")
        assert options.highlighted == 4
        await pilot.press("left")
        assert options.highlighted == 1
        await pilot.press("left")
        assert options.highlighted == 4
        await pilot.press("right")
        assert options.highlighted == 1
        await pilot.press("down")
        assert options.highlighted == 4
        await pilot.press("up")
        assert options.highlighted == 1

        app.screen.query_one("#choice-filter", Input).value = "Archive"
        await pilot.pause()

        assert options.option_count == 2
        assert options.get_option_at_index(0).prompt.plain == "Archive"
        assert options.highlighted == 1

        await pilot.press("enter", "enter")
        await pilot.pause()
        assert results == ["yesterday"]


@pytest.mark.asyncio
async def test_group_shortcuts_select_first_enabled_choice_after_disabled_rows() -> None:
    app = App()

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.push_screen(
            ChoiceScreen(
                title="GROUPS",
                placeholder="Filter choices…",
                choices=(
                    Choice("locked", "Locked", group="First", disabled=True),
                    Choice("available", "Available", group="First"),
                    Choice("second", "Second", group="Second"),
                ),
            )
        )
        await pilot.pause()

        options = app.screen.query_one(OptionList)
        assert options.get_option_at_index(options.highlighted).id == "available"
        await pilot.press("right")
        assert options.get_option_at_index(options.highlighted).id == "second"
        await pilot.press("right")
        assert options.get_option_at_index(options.highlighted).id == "available"


@pytest.mark.asyncio
async def test_refreshing_choice_screen_can_retain_focus_position_across_groups() -> None:
    app = App()

    async def move_selected(_selected: str) -> tuple[Choice, ...]:
        return (
            Choice("first", "First", group="Current"),
            Choice("third", "Third", group="Current"),
            Choice("second", "Second", group="Archived"),
        )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.push_screen(
            ChoiceScreen(
                title="SESSIONS",
                placeholder="Filter sessions…",
                choices=(
                    Choice("first", "First", group="Current"),
                    Choice("second", "Second", group="Current"),
                    Choice("third", "Third", group="Archived"),
                ),
                initial_value="second",
                on_select=move_selected,
                retain_focus_position_on_select=True,
            )
        )
        await pilot.pause()

        options = app.screen.query_one(OptionList)
        await pilot.press("enter")
        await pilot.pause()

        assert app.screen.query_one(OptionList) is options
        assert options.highlighted is not None
        assert options.get_option_at_index(options.highlighted).id == "third"


@pytest.mark.asyncio
async def test_grouped_choice_refresh_preserves_rows_and_refreshes_header_status() -> None:
    app = App()
    first = Choice("first", "First", group="Current", group_trailing="1 item")
    second = Choice("second", "Second", group="Archive")
    async with app.run_test(size=(80, 24)) as pilot:
        menu = ChoiceScreen(
            title="GROUPS",
            placeholder="Filter choices…",
            choices=(first, second),
            initial_value="second",
        )
        app.push_screen(menu)
        await pilot.pause()
        options = menu.query_one(OptionList)
        original_options = tuple(options.options)
        updated = replace(first, label="Renamed", marker="●")
        menu.update_choices((updated, second), preferred_value="second")
        await pilot.pause()
        assert all(
            before is after for before, after in zip(original_options, options.options, strict=True)
        )
        assert options.get_option("first").prompt.plain == "● Renamed"
        assert options.get_option_at_index(options.highlighted).id == "second"

        menu.update_choices(
            (replace(updated, group_trailing="Updated"), second),
            preferred_value="second",
        )
        await pilot.pause()
        header = options.get_option_at_index(0)
        rendered = "".join(segment.text for segment in Console(width=60).render(header.prompt))
        assert "Current" in rendered and "Updated" in rendered and "1 item" not in rendered
        assert options.get_option_at_index(options.highlighted).id == "second"


@pytest.mark.asyncio
async def test_retained_focus_clears_when_refresh_filter_has_no_choices() -> None:
    app = App()

    async def archive_selected(_selected: str) -> tuple[Choice, ...]:
        return (Choice("only", "Only", group="Archived"),)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.push_screen(
            ChoiceScreen(
                title="SESSIONS",
                placeholder="Filter sessions…",
                choices=(Choice("only", "Only", group="Current"),),
                on_select=archive_selected,
                retain_focus_position_on_select=True,
            )
        )
        await pilot.pause()

        app.screen.query_one(Input).value = "Current"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        options = app.screen.query_one(OptionList)
        assert options.option_count == 0
        assert options.highlighted is None


@pytest.mark.asyncio
async def test_choice_page_refreshes_parent_before_in_place_back_navigation() -> None:
    app = App()
    label = "Before"

    async def refresh_parent() -> tuple[Choice, ...]:
        return (Choice("item", label),)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = ChoiceScreen(
            title="ITEMS",
            placeholder="Filter items…",
            choices=(Choice("item", label),),
            on_refresh=refresh_parent,
        )
        app.push_screen(screen)
        await pilot.pause()
        options = screen.query_one(OptionList)
        screen.push_page(
            title="ITEM",
            placeholder="Choose an action…",
            choices=(Choice("edit", "Edit"),),
        )
        label = "After"

        await pilot.press("escape")
        await pilot.pause()

        assert app.screen is screen
        assert screen.query_one(OptionList) is options
        assert screen._title == "ITEMS"
        assert screen._choices == (Choice("item", "After"),)
        assert screen._page_stack == []


@pytest.mark.asyncio
async def test_startup_resume_failure_opens_choice_dialog_and_keeps_tui_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    session = application.runtime.sessions.create(title="Damaged startup session").session
    app = OuroTuiApp(
        application=application,
        resume_session_id=session.session_id,
    )

    async def fail_load(_session_id: str) -> None:
        raise PersistenceError("session payload is incompatible")

    monkeypatch.setattr(app._controller, "prepare_session_selection", fail_load)
    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()

        assert isinstance(app.screen, ChoiceScreen)
        assert app.screen._title == "SESSION LOAD FAILED"
        error = app.screen._choices[0].description
        assert session.session_id in error
        assert "session payload is incompatible" in error

        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(app.screen, ChoiceScreen)
        assert app.query_one(Composer).disabled is False
        assert app.is_running is True


@pytest.mark.asyncio
async def test_actions_persists_three_distinct_recent_choices_across_restart(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        for label, screen_type, title in (
            ("Theme", ChoiceScreen, "THEME"),
            ("Context compaction", CompactionConfigurationScreen, "CONTEXT COMPACTION"),
            ("+ mcp", MCPConfigurationScreen, "ADD MCP"),
        ):
            await pilot.press("ctrl+p")
            await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
            assert "Recent" not in {choice.group for choice in app.screen._choices}
            await choose_menu(pilot, label, title=title)
            assert isinstance(app.screen, screen_type)
            await pilot.press("escape")
            await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
            await pilot.press("escape")
            await _wait_until(
                lambda: not isinstance(app.screen, ChoiceScreen),
                pilot,
                description="actions menu closed",
            )

        preferences = TuiPreferencesStore(tmp_path).load()
        assert len(preferences.recent_actions) == 3

        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert [choice.label for choice in app.screen._choices[:3]] == [
            "+ mcp",
            "Context compaction",
            "Theme",
        ]
        assert all(choice.group == "Recent" for choice in app.screen._choices[:3])

    restarted = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    async with run_ready_app(restarted, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(restarted, ChoiceScreen, title="ACTIONS")
        assert [choice.label for choice in restarted.screen._choices[:3]] == [
            "+ mcp",
            "Context compaction",
            "Theme",
        ]
        assert all(choice.group == "Recent" for choice in restarted.screen._choices[:3])


@pytest.mark.asyncio
async def test_plugin_actions_reuse_grouped_menu_and_refresh_lifecycle_status(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34), notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Manage plugins"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PLUGINS")

        plugin_screen = app.screen
        assert list(dict.fromkeys(choice.group for choice in plugin_screen._choices)) == [
            "Built-in"
        ]
        agent_context = next(
            choice for choice in plugin_screen._choices if choice.value == "dev.ouro.agent_context"
        )
        assert agent_context.label == "Agent Context"
        assert agent_context.trailing == "v1.0.0 • active"
        assert agent_context.marker == ""

        plugin_screen.query_one("#choice-filter", Input).value = "Agent Context"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PLUGIN • Agent Context")
        assert app.screen is plugin_screen
        description = plugin_screen.query_one("#choice-description", Static)
        detail = await app._controller.plugin("dev.ouro.agent_context")
        assert description.display and str(description.content) == detail.description
        assert [choice.label for choice in plugin_screen._choices] == [
            "Disable",
            "Check",
            "Restart",
        ]
        assert (
            next(choice for choice in plugin_screen._choices if choice.value == "restart").disabled
            is False
        )

        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PLUGIN • Agent Context")
        assert app.screen is plugin_screen
        await _wait_until(
            lambda: any(
                choice.value == "enable" and choice.trailing == "disabled"
                for choice in plugin_screen._choices
            ),
            pilot,
            description="disabled plugin actions",
        )
        lifecycle = {choice.value: choice for choice in plugin_screen._choices}
        assert lifecycle["enable"].trailing == "disabled"
        assert lifecycle["restart"].disabled is True
        assert "Agent Context" not in app._system_status.plugin_active_names

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="PLUGINS")
        assert not description.display
        refreshed = next(
            choice for choice in plugin_screen._choices if choice.value == "dev.ouro.agent_context"
        )
        assert refreshed.trailing == "v1.0.0 • disabled"
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PLUGIN • Agent Context")
        options = plugin_screen.query_one("#choice-list", OptionList)
        await pilot.click(options, offset=(4, 1))
        await _wait_for_screen(app, ChoiceScreen, title="PLUGIN • Agent Context")
        await _wait_until(
            lambda: any(
                choice.value == "disable" and choice.trailing == "active"
                for choice in plugin_screen._choices
            ),
            pilot,
            description="enabled plugin actions",
        )
        assert (
            next(choice for choice in plugin_screen._choices if choice.value == "disable").trailing
            == "active"
        )
        assert (
            next(choice for choice in plugin_screen._choices if choice.value == "restart").disabled
            is False
        )
        assert (
            plugin_screen.query_one("#choice-list", OptionList).get_option("restart").disabled
            is False
        )

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="PLUGINS")
        refreshed = next(
            choice for choice in plugin_screen._choices if choice.value == "dev.ouro.agent_context"
        )
        assert refreshed.marker == ""
        assert refreshed.trailing == "v1.0.0 • active"


@pytest.mark.asyncio
async def test_configuration_choice_submenus_reuse_action_menu_without_flash(
    tmp_path: Path,
) -> None:
    saved = ConfigurationFacade(tmp_path).set_mcp_server_by_name(
        "local",
        MCPServerConfigUpdate(command="python", auto_start=False),
    )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        action_screen = app.screen

        action_screen.query_one("#choice-filter", Input).value = "Manage MCPs"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCPs")
        assert app.screen is action_screen
        assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1

        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCP • local")
        assert app.screen is action_screen
        assert [choice.value for choice in app.screen._choices] == [
            "edit",
            "toggle",
            "check",
            "start",
            "refresh",
            "restart",
            "stop",
            "remove",
        ]
        toggle = next(choice for choice in app.screen._choices if choice.value == "toggle")
        assert toggle.label == "Disable"

        options = app.screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("toggle")
        options.focus()
        await pilot.press("enter")
        await wait_for_menu_action(pilot, action_screen)
        assert ConfigurationFacade(tmp_path).mcp_server(saved.server_id).enabled is False
        assert (
            next(choice for choice in app.screen._choices if choice.value == "toggle").label
            == "Enable"
        )
        start = next(choice for choice in app.screen._choices if choice.value == "start")
        assert start.label == "Connect"
        assert start.disabled is True
        assert options.get_option("start").disabled is True
        assert all(
            next(choice for choice in app.screen._choices if choice.value == action).disabled
            for action in ("refresh", "restart", "stop")
        )
        assert all(options.get_option(action).disabled for action in ("refresh", "restart", "stop"))

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="MCPs")
        assert (
            next(
                choice for choice in app.screen._choices if choice.value == saved.server_id
            ).trailing
            == "disabled"
        )
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCP • local")
        options = app.screen.query_one("#choice-list", OptionList)
        await pilot.click(options, offset=(4, 1))
        await wait_for_menu_action(pilot, action_screen)
        assert ConfigurationFacade(tmp_path).mcp_server(saved.server_id).enabled is True
        assert (
            next(choice for choice in app.screen._choices if choice.value == "toggle").label
            == "Disable"
        )
        assert (
            next(choice for choice in app.screen._choices if choice.value == "start").disabled
            is False
        )

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="MCPs")
        assert app.screen is action_screen
        assert (
            next(
                choice for choice in app.screen._choices if choice.value == saved.server_id
            ).trailing
            == "configured"
        )
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert app.screen is action_screen


@pytest.mark.asyncio
async def test_action_choice_submenus_reuse_the_action_screen_without_flash(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    application.runtime.sessions.create(
        session_id="ses_navigation",
        title="Navigation session",
    )
    app = OuroTuiApp(home=tmp_path, application=application)

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        for action_label, title in (
            ("Theme", "THEME"),
            ("Open session", "SESSION • USER"),
            ("Archive session", "ARCHIVE SESSIONS • USER"),
            ("Keyboard help", "KEYBOARD HELP"),
        ):
            await pilot.press("ctrl+p")
            await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
            action_screen = app.screen
            action_screen.query_one("#choice-filter", Input).value = action_label
            await pilot.pause()
            await pilot.press("enter")
            await _wait_for_screen(app, ChoiceScreen, title=title)

            assert app.screen is action_screen
            assert len(app.screen_stack) == 2

            await pilot.press("escape")
            await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
            assert app.screen is action_screen
            filter_input = action_screen.query_one("#choice-filter", Input)
            assert filter_input.value == action_label
            assert filter_input.cursor_position == len(action_label)
            assert filter_input.selection.is_empty
            assert filter_input.has_focus
            assert app.cursor_position == filter_input.cursor_screen_offset
            await pilot.press("x")
            assert filter_input.value == action_label + "x"
            await pilot.press("escape")


@pytest.mark.asyncio
async def test_tool_execution_uses_shared_grouped_inline_menu(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    results = []

    async with run_ready_app(app, size=(80, 24)) as pilot:
        await pilot.pause()
        current = await app._controller.tool_execution_configuration()
        screen = ToolExecutionConfigurationScreen(current)
        app.push_screen(screen, callback=results.append)
        await pilot.pause()

        assert isinstance(screen, EditableChoiceScreen)
        assert not list(screen.query("Button"))
        assert not list(screen.query("Checkbox"))
        assert not list(screen.query("Select"))
        assert [choice.group for choice in screen._choices] == [
            "EXECUTION",
            "EXECUTION",
            "EXECUTION",
            "PATH ACCESS",
            "ACTIONS",
        ]

        options = screen.query_one("#choice-list", OptionList)
        save_calls: list[tuple[str, str]] = []
        save_field = screen._on_save

        async def track_save(field_id: str, value: str) -> tuple[Choice, ...] | None:
            save_calls.append((field_id, value))
            return await save_field(field_id, value)

        screen._on_save = track_save
        await pilot.click(options, offset=(4, 2))
        await pilot.pause()
        assert screen._editing_value == "tool-max-concurrency"
        assert screen._edit_cursor_timer is not None
        assert screen._edit_cursor_visible is True

        screen._toggle_edit_cursor()
        assert screen._edit_cursor_visible is False
        screen.action_edit_home()
        assert screen._edit_cursor_visible is True

        await pilot.press("enter")
        await pilot.pause()
        assert screen._editing_value is None
        assert screen._edit_cursor_timer is None
        assert save_calls == []

        screen._refresh("tool-execution-mode")
        await pilot.press("enter")
        await pilot.pause()
        assert screen._values["tool-execution-mode"] != current.mode

        screen._refresh("tool-workspace-only")
        await pilot.press("enter")
        await pilot.pause()
        assert screen._workspace_only is not current.workspace_only

        screen._refresh("tool-config-save")
        await pilot.press("enter")
        await pilot.pause()
        assert len(results) == 1
        assert results[0].mode != current.mode
        assert results[0].workspace_only is not current.workspace_only


@pytest.mark.asyncio
async def test_compaction_uses_shared_grouped_inline_menu_and_returns_typed_draft(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    results = []

    async with run_ready_app(app, size=(100, 42)) as pilot:
        await pilot.pause()
        runtime_models = await app._controller.models()
        unsupported_model = runtime_models[0]
        capable_model = unsupported_model.model_copy(
            update={
                "key": "faux/thinking",
                "provider_id": "faux",
                "endpoint_id": "thinking",
                "display_name": "Thinking model",
                "reasoning": ReasoningCapability(
                    activation="toggle",
                    supported_efforts=("low", "high"),
                    default=ReasoningRequest(mode="enabled", effort="low"),
                ),
            }
        )
        models = (unsupported_model, capable_model)
        screen = CompactionConfigurationScreen(
            await app._controller.compaction_configuration(),
            models=models,
            strategy_ids=app._controller.compaction_strategies(),
        )
        app.push_screen(screen, callback=results.append)
        await pilot.pause()
        assert isinstance(screen, EditableChoiceScreen)
        assert not list(screen.query("Button"))
        assert not list(screen.query("Checkbox"))
        assert not list(screen.query("Select"))
        assert [choice.group for choice in screen._choices] == [
            "STRATEGY",
            "STRATEGY",
            "STRATEGY",
            "THRESHOLDS",
            "THRESHOLDS",
            "MODEL",
            "MODEL",
            "MODEL",
            "MODEL",
            "ACTIONS",
        ]

        choices = {choice.value: choice for choice in screen._choices}
        assert choices["compaction-strategy"].disabled is True
        assert choices["compaction-fallback"].disabled is True
        assert choices["compaction-temperature"].disabled is False
        assert choices["compaction-max-output"].disabled is False
        assert choices["compaction-max-output"].label == "Max output tokens"
        assert choices["compaction-max-output"].trailing == "Optional"
        assert choices["compaction-max-output"].edit_placeholder == ""
        assert choices["compaction-reasoning"].disabled is False

        screen._refresh("compaction-model")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        assert app.screen is not screen
        assert app.screen.query_one("#choice-filter", Input).placeholder == "Filter models…"
        model_choices = app.screen._choices
        assert model_choices[0].group == "MODEL SOURCE"
        assert model_choices[1].group == "MODEL SOURCE"
        assert model_choices[1].disabled is True
        screen._system_default_model = capable_model
        assert screen._model_choices()[1].disabled is False
        assert {choice.group for choice in model_choices[2:]} == {
            "Other models",
        }
        app.screen.query_one("#choice-filter", Input).value = "Thinking model"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is screen
        assert screen._values["compaction-model"] == capable_model.key
        choices = {choice.value: choice for choice in screen._choices}
        assert choices["compaction-temperature"].disabled is False
        assert choices["compaction-max-output"].disabled is False
        assert choices["compaction-reasoning"].disabled is False

        screen._values["compaction-trigger"] = "0.4"
        screen._values["compaction-target"] = "0.5"
        screen._refresh("compaction-save")
        save = next(choice for choice in screen._choices if choice.value == "compaction-save")
        assert save.disabled is True
        assert "lower than" in save.trailing

        screen._values["compaction-target"] = "0.2"
        screen._refresh("compaction-save")
        await pilot.press("enter")
        await pilot.pause()
        assert len(results) == 1
        assert results[0].trigger_ratio == 0.4
        assert results[0].target_ratio == 0.2
        assert results[0].model_key == capable_model.key
