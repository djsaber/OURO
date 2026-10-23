"""TUI application scenarios: commands."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.widgets import Input, OptionList, Static

from ouro.application.contracts.plugins import PluginCommandExecutionView, PluginCommandView
from ouro.core.events import EventRecord
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.composer.popup import CommandPopup
from ouro.tui.conversation.history import ConversationHistory
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.preferences import TuiPreferences, TuiPreferencesStore
from ouro.tui.state import TuiSessionProjection
from ouro.tui.view_models import TuiPluginCommandExecution
from ouro.tui.widgets.status import StatusLine
from tests.tui.app.support import _application
from tests.tui.helpers import (
    command_popup_text,
    resize_terminal,
    run_ready_app,
    wait_for_menu_action,
)
from tests.tui.helpers import wait_for_screen as _wait_for_screen
from tests.tui.helpers import wait_for_worker_group as _wait_for_worker_group
from tests.tui.helpers import wait_until as _wait_until


@pytest.mark.asyncio
async def test_fork_command_uses_inline_targets_for_full_and_user_boundary(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 34), notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("f", "o", "r", "k", "enter")
        await _wait_until(
            lambda: (
                app._controller.session_id is not None
                and app._projection is not None
                and app._projection.activity.status == "completed"
            ),
            pilot,
            description="source session",
        )
        source_session_id = app._controller.session_id
        assert source_session_id is not None
        source = application.runtime.store.load_session(source_session_id)

        await pilot.press("slash", *"fork", "enter")
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/fork"
        assert [argument.value for argument in popup._arguments] == ["full", "#1"]
        assert [argument.display_label for argument in popup._arguments] == [
            "Full",
            "#1",
        ]
        popup_text = command_popup_text(popup).plain
        assert "Fork the complete current session" in popup_text
        assert "fork" in popup_text
        assert not isinstance(app.screen, ChoiceScreen)
        assert app._controller.session_id == source_session_id

        await pilot.press("enter")
        await _wait_until(
            lambda: app._controller.session_id not in {None, source_session_id},
            pilot,
            description="fully forked session",
        )
        full_session_id = app._controller.session_id
        assert full_session_id is not None
        full_branch = application.runtime.store.load_session(full_session_id)
        assert full_branch.session.parent_session_id == source_session_id
        assert full_branch.session.forked_from_message_id == source.messages[-1].message_id
        assert [message.text_content for message in full_branch.messages] == [
            message.text_content for message in source.messages
        ]

        await pilot.press("slash", *"fork", "enter", "down", "enter")
        await _wait_until(
            lambda: app._controller.session_id not in {None, full_session_id},
            pilot,
            description="user-boundary forked session",
        )
        boundary_session_id = app._controller.session_id
        assert boundary_session_id is not None
        boundary_branch = application.runtime.store.load_session(boundary_session_id)
        assert boundary_branch.session.parent_session_id == full_session_id
        assert boundary_branch.session.forked_from_message_id is None
        assert boundary_branch.messages == []
        await _wait_until(
            lambda: app.query_one(Composer).text == "fork",
            pilot,
            description="forked user input restored",
        )
        assert app.query_one(Composer).has_focus is True
        assert not isinstance(app.screen, ChoiceScreen)
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))


@pytest.mark.asyncio
async def test_fork_command_refuses_active_tui_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    app = OuroTuiApp(application=application)

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("f", "o", "r", "k", "enter")
        await _wait_until(
            lambda: (
                app._controller.session_id is not None
                and app._projection is not None
                and app._projection.activity.status == "completed"
            ),
            pilot,
            description="source session",
        )
        source_session_id = app._controller.session_id
        assert source_session_id is not None
        assert app._projection is not None
        app._projection.activity = app._projection.activity.model_copy(update={"status": "running"})

        def unexpected_branch(_message_id: str | None = None) -> None:
            raise AssertionError("active TUI session reached the branch service")

        monkeypatch.setattr(app._controller, "branch_session", unexpected_branch)
        app._fork_session("full")
        await pilot.pause()

        assert app._controller.session_id == source_session_id
        assert not isinstance(app.screen, ChoiceScreen)


@pytest.mark.asyncio
async def test_root_help_groups_runtime_plugin_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app._controller,
            "plugin_commands",
            lambda: (
                PluginCommandView(
                    command_id="dev.ouro.demo:hello",
                    plugin_id="dev.ouro.demo",
                    name="hello",
                    description="Say hello",
                    usage="<name>",
                ),
            ),
        )

        await pilot.press("f1")
        await _wait_for_screen(app, ChoiceScreen, title="KEYBOARD HELP")

        plugins = tuple(
            choice for choice in app.screen._choices if choice.group == "PLUGIN COMMANDS"
        )
        assert [(choice.label, choice.trailing) for choice in plugins] == [
            ("/hello <name>", "Say hello")
        ]

        help_screen = app.screen
        help_screen.query_one("#choice-filter", Input).value = "@"
        await pilot.pause()
        options = help_screen.query_one("#choice-list", OptionList)
        assert [
            option.id
            for index in range(options.option_count)
            if not (option := options.get_option_at_index(index)).disabled
        ] == ["command:@file", "command:@skill"]
        await pilot.press("enter")
        assert app.screen is help_screen

        for key, title in (
            ("ctrl+p", "ACTIONS"),
            ("ctrl+o", "SESSION • USER"),
            ("ctrl+t", "Tasks"),
            ("ctrl+s", "SKILLS"),
            ("alt+p", "PLUGINS"),
        ):
            await pilot.press(key)
            await _wait_for_screen(app, ChoiceScreen, title=title)
            assert app.screen is help_screen
            assert len(app.screen_stack) == 2
            await pilot.press("f1")
            await _wait_for_screen(app, ChoiceScreen, title="KEYBOARD HELP")
            assert app.screen is help_screen
            assert len(app.screen_stack) == 2

        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        help_screen.query_one(Input).value = "New session"
        await pilot.pause()
        await pilot.press("enter")
        await wait_for_menu_action(pilot, help_screen)
        assert len(app.screen_stack) == 1
        assert app._active_title == "New conversation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "data", "expected_kind", "visible_text"),
    (
        ("hello", {}, "notice", "▣ hello"),
        ("Goal: active\n```\n**literal objective**\n```", {}, "command", "**literal objective**"),
        ("", {"objective": "检查命令内容"}, "command", '"objective": "检查命令内容"'),
    ),
)
async def test_plugin_commands_complete_and_execute_through_controller_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    data: dict[str, str],
    expected_kind: str,
    visible_text: str,
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
            execution=PluginCommandExecutionView(
                command_id="dev.ouro.demo:hello", output=output, data=data
            )
        )

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app._controller,
            "plugin_commands",
            lambda: (
                PluginCommandView(
                    command_id="dev.ouro.demo:hello",
                    plugin_id="dev.ouro.demo",
                    name="hello",
                    description="Say hello",
                    usage="<message>",
                ),
            ),
        )
        monkeypatch.setattr(app._controller, "execute_plugin_command", execute)

        await pilot.press("slash")
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        popup_text = command_popup_text(popup).plain
        assert "/hello" in popup_text
        app.query_one(Composer).clear()

        await pilot.press("slash", *"hello world", "enter")
        await _wait_for_worker_group(app, pilot, group="plugin-command")

        assert executed == [("hello", "world")]
        history = app.query_one(ConversationHistory)
        assert history.entries[-1].kind == expected_kind
        assert history.entries[-1].text == (
            output or json.dumps(data, ensure_ascii=False, indent=2)
        )
        assert visible_text in "\n".join(line.text for line in history.lines)
        content_segment = next(
            segment
            for line in history.lines
            for segment in line
            if visible_text.removeprefix("▣ ") in segment.text
        )
        assert content_segment.style is not None
        assert not content_segment.style.bold
        if expected_kind == "command":
            assert history.entries[-1].created_at is None
            assert history.user_turns == ()


@pytest.mark.asyncio
async def test_abort_and_quit_commands_have_independent_lifecycles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    aborted = False

    async def fake_abort() -> None:
        nonlocal aborted
        aborted = True

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app._controller, "abort", fake_abort)
        app._projection = TuiSessionProjection("session_1")
        app._projection.apply(
            EventRecord(
                session_id="session_1",
                run_id="run_1",
                event_type="run_started",
                sequence=1,
            )
        )

        await app._dispatch_command("/abort")
        await pilot.pause()

        assert aborted is True
        assert app.is_running is True

        await app._dispatch_command("/quit")
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.asyncio
async def test_command_and_approval_menus_fit_resized_terminal(tmp_path: Path) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("start session")
        await pilot.press("enter")
        await _wait_for_worker_group(app, pilot, group="run")

        await resize_terminal(pilot, 80, 16)
        assert await pilot.click("#composer")
        await pilot.press("slash")
        await pilot.pause()
        popup = app.query_one(CommandPopup)
        assert popup.region.height == 9
        assert popup.region.bottom == composer.region.y
        assert app.query_one(StatusLine).region.bottom == app.screen.region.bottom

        await resize_terminal(pilot, 40, 12)
        await pilot.pause()
        assert popup.region.height == 5
        assert popup.region.right <= app.screen.region.right
        assert popup.region.bottom == composer.region.y
        assert app.query_one(StatusLine).region.bottom == app.screen.region.bottom

        composer.load_text("/approval ")
        composer.move_cursor((0, len(composer.text)))
        await pilot.pause()
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/approval"
        assert popup.region.bottom == composer.region.y
        assert popup.region.right <= app.screen.region.right

        for _ in range(len(popup._arguments) - 1):
            await pilot.press("down")
        await pilot.pause()
        assert popup.selected_argument == popup._arguments[-1]
        assert popup.scroll_y > 0

        await resize_terminal(pilot, 100, 32)
        composer.load_text("/")
        composer.move_cursor((0, 1))
        await pilot.pause()
        assert popup.region.height == 14
        assert popup.region.bottom == composer.region.y


@pytest.mark.asyncio
async def test_workspace_command_selects_paths_only_before_session_and_keeps_clear_available(
    tmp_path: Path,
) -> None:
    historical = tmp_path / "parent" / "historical"
    freeform = tmp_path / "custom workspace"
    rejected = tmp_path / "rejected"
    for workspace in (historical, freeform, rejected):
        workspace.mkdir(parents=True)
    preferences = TuiPreferencesStore(tmp_path)
    preferences.save(TuiPreferences(recent_workspaces=(str(historical.resolve()),)))
    application = _application(tmp_path)
    app = OuroTuiApp(home=tmp_path, application=application)

    async with run_ready_app(app, size=(110, 34)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        await pilot.press("slash", *"workspace", "enter")
        await pilot.pause()

        popup = app.query_one(CommandPopup)
        assert len(app.screen_stack) == 1
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/workspace"
        assert [argument.value for argument in popup._arguments] == [
            str(tmp_path.resolve()),
            str(historical.resolve()),
            "clear",
        ]
        assert [argument.display_label for argument in popup._arguments] == [
            str(tmp_path.resolve()),
            str(historical.resolve()),
            "clear",
        ]
        assert all(not argument.description for argument in popup._arguments)
        workspace_menu = command_popup_text(popup)
        assert "Current workspace" not in workspace_menu.plain
        assert "Recent workspace" not in workspace_menu.plain

        await pilot.press("down", "enter")
        # The header changes before the off-thread history save completes.
        await _wait_for_worker_group(app, pilot, group="composer-command", timeout=15)
        assert app._active_workspace == str(historical.resolve())
        assert preferences.load().recent_workspaces[:2] == (
            str(historical.resolve()),
            str(tmp_path.resolve()),
        )

        composer = app.query_one(Composer)
        for parent in (historical.parent, tmp_path):
            composer.load_text(f"/workspace {parent}")
            composer.move_cursor((0, len(composer.document.lines[0])))
            await pilot.pause()
            assert str(historical.resolve()) in [item.value for item in popup._arguments]
            assert popup.selected_argument is None

            await pilot.press("enter")
            await _wait_for_worker_group(app, pilot, group="composer-command", timeout=15)
            assert app._active_workspace == str(parent.resolve())
            assert preferences.load().recent_workspaces[0] == str(parent.resolve())

        composer.load_text(f"/workspace {historical.parent}")
        composer.move_cursor((0, len(composer.document.lines[0])))
        await pilot.pause()
        await pilot.press("up")
        assert popup.selected_argument is not None
        assert popup.selected_argument.value == str(historical.resolve())
        await pilot.press("enter")
        await _wait_for_worker_group(app, pilot, group="composer-command", timeout=15)
        assert app._active_workspace == str(historical.resolve())

        composer.load_text(f"/workspace {historical.parent}")
        composer.move_cursor((0, len(composer.document.lines[0])))
        await pilot.pause()
        await pilot.press("down")
        assert popup.selected_argument is not None
        assert popup.selected_argument.value == str(historical.resolve())
        await pilot.press("right")
        await pilot.pause()
        assert composer.text == f"/workspace {historical}"
        assert popup.selected_argument is None
        await pilot.press(*("backspace" for _ in historical.name))
        await pilot.pause()
        assert popup.selected_argument is None
        await pilot.press("enter")
        await _wait_for_worker_group(app, pilot, group="composer-command", timeout=15)
        assert app._active_workspace == str(historical.parent.resolve())

        composer.load_text(f"/workspace {historical}")
        composer.move_cursor((0, len(composer.document.lines[0])))
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_worker_group(app, pilot, group="composer-command", timeout=15)
        assert app._active_workspace == str(historical.resolve())

        composer.load_text(f"/workspace {freeform}")
        composer.move_cursor((0, len(composer.document.lines[0])))
        await pilot.pause()
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/workspace"
        assert popup.selected_argument is None
        assert not popup.display

        await pilot.press("enter")
        await _wait_for_worker_group(app, pilot, group="composer-command", timeout=15)
        assert app._active_workspace == str(freeform.resolve())
        assert preferences.load().recent_workspaces[:4] == (
            str(freeform.resolve()),
            str(historical.resolve()),
            str(historical.parent.resolve()),
            str(tmp_path.resolve()),
        )

        composer.load_text("create the session")
        await pilot.press("enter")
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="workspace session ready for commands",
        )
        session_id = app._controller.session_id
        assert session_id is not None
        session = application.runtime.store.load_session_record(session_id)
        assert session.workspace == str(freeform.resolve())
        assert "/workspace" in {command.name for command in app._command_catalog()}

        composer.load_text("/workspace ")
        composer.move_cursor((0, len(composer.document.lines[0])))
        await pilot.pause()
        assert [argument.value for argument in popup._arguments] == ["clear"]
        assert popup.argument_command is not None
        assert popup.argument_command.input_hint is None

        await app._dispatch_command(f"/workspace {rejected}")
        await pilot.pause()
        unchanged = application.runtime.store.load_session_record(session_id)
        assert unchanged.workspace == str(freeform.resolve())


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ("actions", "menu"))
async def test_actions_command_opens_action_menu(tmp_path: Path, command: str) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.press("slash", *command, "enter")
        await pilot.pause()

        assert isinstance(app.screen, ChoiceScreen)
        assert app.screen.query_one("#choice-title", Static).render().plain == "ACTIONS"
