"""TUI application scenarios: command popup."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from ouro.application.contracts.plugins import (
    PluginCommandArgumentView,
    PluginCommandExecutionView,
    PluginCommandView,
)
from ouro.tui.app import OuroTuiApp
from ouro.tui.commands import TuiCommand, TuiCommandArgument
from ouro.tui.composer.input import Composer
from ouro.tui.composer.popup import CommandPopup
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.view_models import TuiPluginCommandExecution
from tests.tui.app.support import _application
from tests.tui.helpers import command_popup_text, run_ready_app


@pytest.mark.asyncio
async def test_slash_popup_loads_active_runtime_plugin_commands(tmp_path: Path) -> None:
    application = _application(tmp_path)
    await application.start()
    assert application.plugins is not None
    enabled = await application.plugins.enable("dev.ouro.goal")
    assert enabled.status == "active"
    goal = next(item for item in application.plugins.list_commands() if item.name == "goal")
    assert [item.value for item in goal.argument_options] == [
        "status",
        "pause",
        "resume",
        "cancel",
        "replace",
    ]
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()

        popup = app.query_one(CommandPopup)
        content = command_popup_text(popup).plain
        assert "/goal" in content


@pytest.mark.asyncio
async def test_slash_prefix_filters_new_command_text_and_restores_existing_draft(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        popup = app.query_one(CommandPopup)
        draft = "Compare these two implementations"
        composer.load_text(draft)
        composer.move_cursor((0, 0))
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()

        assert composer.text == f"/{draft}"
        assert popup.display is True
        assert len(popup._commands) == len(app._command_catalog())

        await pilot.press(*"model")
        await pilot.pause()

        assert popup.selected_command is not None
        assert popup.selected_command.name == "/model"
        assert composer.text == f"/model{draft}"

        await pilot.press("enter")
        await pilot.pause()

        assert popup.argument_command is not None
        assert popup.argument_command.name == "/model"
        assert composer.text == f"/model {draft}"

        await pilot.press("enter")
        await pilot.pause()

        assert popup.display is False
        assert composer.text == draft

        composer.move_cursor((0, 0))
        await pilot.press("slash", *"not-a-command")
        await pilot.pause()
        assert popup.display is False

        await pilot.press("escape")
        await pilot.pause()
        assert composer.text == draft


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        "/not-a-command",
        "/model 你叫什么名字",
        "/model faux/faux extra text",
        "/help extra text",
        "/plan unknown",
        "/skill missing explain this",
    ),
)
@pytest.mark.parametrize("key, mode", (("enter", "follow_up"), ("ctrl+g", "steer")))
async def test_command_popup_hides_when_no_commands_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, key: str, mode: str
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    submitted = []
    monkeypatch.setattr(
        app,
        "_submit_run",
        lambda text, mode, *, attachments, skills: submitted.append((text, mode, attachments)),
    )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press(*text)
        await pilot.pause()

        popup = app.query_one(CommandPopup)
        assert popup.display is False
        assert popup.selected_command is None
        assert "No matching commands" not in app.export_screenshot()
        await pilot.press(key)
        await pilot.pause()
        assert submitted == [(text, mode, ())]
        assert app.query_one(Composer).text == ""


@pytest.mark.asyncio
async def test_model_command_selects_from_inline_argument_menu(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32), notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("slash", *"model", "enter")
        await pilot.pause()

        composer = app.query_one(Composer)
        popup = app.query_one(CommandPopup)
        assert not isinstance(app.screen, ChoiceScreen)
        assert composer.text == "/model "
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/model"
        assert popup.selected_argument is not None
        assert popup.selected_argument.value == "faux/faux"

        await pilot.press("enter")
        await pilot.pause()
        assert composer.text == ""
        assert app._active_model == ""
        assert app._controller._initial_model == "faux/faux"
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))


@pytest.mark.asyncio
@pytest.mark.parametrize("is_suggestion", (False, True))
async def test_command_selection_executes_complete_argument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    is_suggestion: bool,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    executed: list[tuple[str, str]] = []

    async def execute(
        selector: str, argument: str, *, on_submitted: Callable[[], None] | None = None
    ) -> TuiPluginCommandExecution:
        executed.append((selector, argument))
        if on_submitted is not None:
            on_submitted()
        return TuiPluginCommandExecution(
            execution=PluginCommandExecutionView(command_id="dev.ouro.plan:plan")
        )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app._controller,
            "plugin_commands",
            lambda: (
                PluginCommandView(
                    command_id="dev.ouro.plan:plan",
                    plugin_id="dev.ouro.plan",
                    name="plan",
                    description="Enter Plan mode",
                    usage="[on|off|status]",
                    argument_options=(
                        PluginCommandArgumentView(value="on", description="Enter Plan mode"),
                        PluginCommandArgumentView(value="off", description="Leave Plan mode"),
                        PluginCommandArgumentView(value="status", description="Show mode status"),
                    ),
                ),
            ),
        )
        monkeypatch.setattr(app._controller, "execute_plugin_command", execute)

        catalog = app._command_catalog()
        monkeypatch.setattr(
            app,
            "_command_catalog",
            lambda: tuple(
                replace(
                    command,
                    argument_options=tuple(
                        replace(option, is_suggestion=is_suggestion)
                        for option in command.argument_options
                    ),
                )
                for command in catalog
            ),
        )

        await pilot.press("slash", "p", "l", "a", "n", "enter")
        await pilot.pause()

        composer = app.query_one(Composer)
        popup = app.query_one(CommandPopup)
        assert composer.text == "/plan "
        assert composer.suggestion == ""
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/plan"
        assert popup.selected_argument is not None
        assert popup.selected_argument.value == "on"
        assert executed == []

        await pilot.press("down", "enter")
        await pilot.pause()
        assert composer.text == ""
        assert executed == [("plan", "off")]

        composer.load_text("/plan o")
        composer.move_cursor((0, len(composer.text)))
        await pilot.pause()
        if is_suggestion:
            assert popup.selected_argument is None
        else:
            assert popup.selected_argument is not None
            assert popup.selected_argument.value == "on"
        assert composer.suggestion == "n"
        await pilot.press("enter")
        await pilot.pause()
        assert executed[-1] == ("plan", "o" if is_suggestion else "on")


@pytest.mark.asyncio
async def test_parameterized_command_opens_argument_menu_and_executes_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    executed: list[tuple[str, str]] = []

    async def execute(
        selector: str, argument: str, *, on_submitted: Callable[[], None] | None = None
    ) -> TuiPluginCommandExecution:
        executed.append((selector, argument))
        if on_submitted is not None:
            on_submitted()
        return TuiPluginCommandExecution(
            execution=PluginCommandExecutionView(command_id="dev.ouro.goal:goal")
        )

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app._controller,
            "plugin_commands",
            lambda: (
                PluginCommandView(
                    command_id="dev.ouro.goal:goal",
                    plugin_id="dev.ouro.goal",
                    name="goal",
                    description="Control a Goal",
                    usage=(
                        "[--token-budget N] <objective> | status | pause | "
                        "resume [--token-budget N] | cancel | "
                        "replace [--token-budget N] <objective>"
                    ),
                    argument_options=(
                        PluginCommandArgumentView(
                            value="status", description="Show the current Goal"
                        ),
                        PluginCommandArgumentView(
                            value="pause", description="Pause the active Goal"
                        ),
                        PluginCommandArgumentView(
                            value="resume", description="Resume a paused Goal"
                        ),
                        PluginCommandArgumentView(
                            value="cancel", description="Cancel the current Goal"
                        ),
                        PluginCommandArgumentView(
                            value="replace",
                            description="Replace the current Goal",
                            usage="[--token-budget N] <objective>",
                        ),
                    ),
                ),
            ),
        )
        monkeypatch.setattr(app._controller, "execute_plugin_command", execute)

        await pilot.press("slash", "g", "o", "a", "l", "space")
        await pilot.pause()

        composer = app.query_one(Composer)
        popup = app.query_one(CommandPopup)
        assert composer.text == "/goal "
        assert composer.suggestion == "--token-budget N <objective>"
        assert popup.argument_command is not None

        await pilot.press("left")
        await pilot.pause()
        assert composer.cursor_location == (0, len("/goal"))
        assert composer.suggestion == ""

        await pilot.press("right")
        await pilot.pause()
        assert composer.text == "/goal "
        assert composer.suggestion == "--token-budget N <objective>"

        await pilot.press("right")
        await pilot.pause()
        assert composer.text == "/goal --token-budget N <objective>"
        assert "[" not in composer.text
        assert "]" not in composer.text

        composer.load_command("/goal ")
        await pilot.pause()
        await pilot.press("p", "a", "u")
        await pilot.pause()
        assert popup.selected_argument is not None
        assert popup.selected_argument.value == "pause"
        assert composer.suggestion == "se"
        content = command_popup_text(popup).plain
        assert "pause" in content
        assert "status" not in content

        await pilot.press("enter")
        await pilot.pause()
        assert composer.text == ""
        assert executed == [("goal", "pause")]

        composer.load_command("/goal Explain this project")
        await pilot.pause()
        assert not popup.display
        await pilot.press("enter")
        await pilot.pause()
        assert executed[-1] == ("goal", "Explain this project")


@pytest.mark.asyncio
async def test_deleting_command_argument_updates_suggestion_in_same_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    hint = "--token-budget N <objective>"

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app,
            "_command_catalog",
            lambda: (
                TuiCommand(
                    "/goal",
                    "Goal",
                    "Control a Goal",
                    "<objective> | replace [--token-budget N] <objective>",
                    argument_options=(
                        TuiCommandArgument(
                            "replace",
                            "Replace the current Goal",
                            "[--token-budget N] <objective>",
                        ),
                    ),
                ),
            ),
        )
        composer = app.query_one(Composer)
        composer.load_command("/goal replace")
        await pilot.pause()
        assert composer.suggestion == f" {hint}"

        transitions: list[tuple[str, str]] = []
        app.watch(
            composer,
            "suggestion",
            lambda old, new: transitions.append((old, new)),
            init=False,
        )

        previous = f" {hint}"
        expected_suggestions = (
            f"e {hint}",
            f"ce {hint}",
            f"ace {hint}",
            f"lace {hint}",
            f"place {hint}",
            f"eplace {hint}",
            "<objective>",
        )
        for expected in expected_suggestions:
            transitions.clear()
            composer.action_delete_left()
            assert composer.suggestion == expected
            await pilot.pause()
            assert composer.suggestion == expected
            assert transitions == [(previous, expected)]
            previous = expected

        transitions.clear()
        composer.action_delete_left()
        assert composer.suggestion == ""
        await pilot.pause()
        assert composer.text == "/goal"
        assert composer.suggestion == ""
        assert transitions == [(previous, "")]

        transitions.clear()
        composer.action_delete_left()
        assert composer.suggestion == ""
        await pilot.pause()
        assert composer.text == "/goa"
        assert composer.suggestion == ""
        assert transitions == []
