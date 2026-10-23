"""TUI application scenarios: mcp."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text
from textual.pilot import Pilot
from textual.widgets import Input, OptionList, Static

from ouro.application.configuration.facade import ConfigurationFacade
from ouro.mcp import MCPManager, MCPRuntimeResource, MCPServerConfig, MCPServerConfigUpdate
from ouro.tui.app import OuroTuiApp
from ouro.tui.features.configuration.mcp import MCPConfigurationScreen
from ouro.tui.features.configuration.workflows import _mcp_management_choices
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.confirmation import ConfirmationScreen
from ouro.tui.menus.editable import EditableChoiceScreen
from ouro.tui.theme import DEFAULT_TUI_THEME
from ouro.tui.view_models import McpServerStatus
from tests.tui.app.providers import LiveMcpFactory, LiveMcpSession
from tests.tui.app.support import (
    _application,
    _application_with_tool_sources,
    _wait_for_welcome_render,
)
from tests.tui.helpers import release_on_exit, run_ready_app, wait_for_menu_action
from tests.tui.helpers import wait_for_screen as _wait_for_screen
from tests.tui.helpers import wait_for_worker_group as _wait_for_worker_group
from tests.tui.helpers import wait_until as _wait_until


@pytest.mark.asyncio
async def test_tui_reconciles_live_mcp_connection_and_tool_status(tmp_path: Path) -> None:
    application = _application(tmp_path)
    session = LiveMcpSession()
    factory = LiveMcpFactory(session)
    manager = MCPManager(
        registry=application.runtime.tools,
        client_factory=factory,
    )
    manager.configure(
        MCPServerConfig(
            server_id="live",
            name="Live MCP",
            command="unused-in-test",
            auto_start=True,
            auto_reconnect=False,
            health_check_interval_seconds=60,
        )
    )
    application.mcp = MCPRuntimeResource(manager)
    app = OuroTuiApp(application=application)

    async with (
        run_ready_app(app, size=(100, 32)) as pilot,
        release_on_exit(session.initialize_gate),
    ):
        welcome = await _wait_for_welcome_render(app, pilot, contains="connecting")
        mcp_label = welcome.query_one("#welcome-mcp-label", Static)
        connecting_label = mcp_label.render().plain
        assert connecting_label.endswith(" MCP")
        assert connecting_label[0] in DEFAULT_TUI_THEME.spinner_frames
        starting_status = welcome.page._system._mcp.render()
        assert "0/1 connected • 0 tool(s) • 1 connecting" in starting_status.plain

        session.initialize_gate.set()
        await _wait_until(
            lambda: "1/1 connected • 1 tool(s)" in welcome.page._system._mcp.render().plain,
            pilot,
            description="MCP connection status",
        )
        connected_status = welcome.page._system._mcp.render()
        assert mcp_label.render().plain == "MCP"
        assert "1/1 connected • 1 tool(s)" in connected_status.plain
        assert "live__search" in application.runtime.tools.names()

        session.tools.append(LiveMcpSession._tool("fetch"))
        factory.tools_changed()
        await _wait_until(
            lambda: "1/1 connected • 2 tool(s)" in welcome.page._system._mcp.render().plain,
            pilot,
            description="refreshed MCP tools",
        )
        assert "1/1 connected • 2 tool(s)" in welcome.page._system._mcp.render().plain
        assert {"live__fetch", "live__search"}.issubset(application.runtime.tools.names())

        factory.connection_lost("connection lost")
        await _wait_until(
            lambda: (
                "0/1 connected • 0 tool(s) • 1 failed" in welcome.page._system._mcp.render().plain
            ),
            pilot,
            description="failed MCP connection status",
        )
        assert "0/1 connected • 0 tool(s) • 1 failed" in welcome.page._system._mcp.render().plain
        assert mcp_label.render().plain == "MCP"
        assert "live__search" not in application.runtime.tools.names()

    assert app._mcp_status_timer is None


async def _open_mcp_management(app: OuroTuiApp, pilot: Pilot, action: str) -> ChoiceScreen:
    await pilot.press("ctrl+p")
    await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
    app.screen.query_one("#choice-filter", Input).value = "Manage MCPs"
    await pilot.pause()
    await pilot.press("enter")
    await _wait_for_screen(app, ChoiceScreen, title="MCPs")
    await pilot.press("enter")
    await _wait_for_screen(app, ChoiceScreen, title="MCP • local")
    menu = app.screen
    assert isinstance(menu, ChoiceScreen)
    options = menu.query_one(OptionList)
    options.highlighted = options.get_option_index(action)
    options.focus()
    return menu


@pytest.mark.asyncio
async def test_mcp_catalog_can_create_edit_and_delete_its_last_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    notices: list[str] = []
    monkeypatch.setattr(app, "notify", lambda message, **_: notices.append(message))
    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        menu = app.screen
        assert [choice.label for choice in menu._choices if choice.group == "MCPs"] == [
            "Manage MCPs",
            "+ mcp",
        ]
        menu.query_one(Input).value = "Manage MCPs"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCPs")
        assert app.screen is menu
        assert [(choice.value, choice.label) for choice in menu._choices] == [("+", "+")]
        await pilot.press("enter")
        await _wait_for_screen(app, MCPConfigurationScreen, title="ADD MCP")
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="MCPs")
        await wait_for_menu_action(pilot, menu)
        assert app.screen is menu
        assert menu.query_one(Input).has_focus
        await pilot.press("enter")
        await _wait_for_screen(app, MCPConfigurationScreen, title="ADD MCP")
        editor = app.screen
        for field, value in (
            ("mcp-name", "Local"),
            ("mcp-target", "python"),
        ):
            options = editor.query_one(OptionList)
            options.highlighted = options.get_option_index(field)
            options.focus()
            await pilot.press("enter", *value, "enter")
        options.highlighted = options.get_option_index("mcp-save")
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCPs")
        # Dismissing the editor restores this page before the parent action
        # finishes saving, reconciling runtime state, and refreshing its rows.
        await wait_for_menu_action(pilot, menu)
        assert app.screen is menu
        server_id = ConfigurationFacade(tmp_path).mcp_server_by_name("Local").server_id
        assert [choice.value for choice in menu._choices] == [server_id, "+"]
        options = menu.query_one(OptionList)
        options.highlighted = options.get_option_index(server_id)
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCP • Local")
        assert app.screen is menu
        await pilot.press("enter")
        await _wait_for_screen(app, MCPConfigurationScreen, title="EDIT MCP")
        editor = app.screen
        options = editor.query_one(OptionList)
        assert all(choice.value != "mcp-server-id" for choice in editor._choices)
        options.highlighted = options.get_option_index("mcp-name")
        options.focus()
        await pilot.press("enter", "ctrl+u", *"Renamed", "enter")
        options.highlighted = options.get_option_index("mcp-save")
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCP • Renamed")
        await wait_for_menu_action(pilot, menu)
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="MCPs")
        await _wait_for_worker_group(app, pilot, group="choice-return")
        assert (
            next(choice for choice in menu._choices if choice.value == server_id).label == "Renamed"
        )
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCP • Renamed")
        delete = next(choice for choice in menu._choices if choice.value == "remove")
        assert delete.label == "Delete"
        assert delete.destructive
        options = menu.query_one(OptionList)
        options.highlighted = options.get_option_index("remove")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, ConfirmationScreen, title="CONFIRM DELETE MCP")
        confirmation = next(choice for choice in app.screen._choices if choice.destructive)
        assert "Delete Renamed?" in confirmation.description
        assert server_id not in confirmation.description
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="MCP • Renamed")
        await wait_for_menu_action(pilot, menu)
        assert ConfigurationFacade(tmp_path).mcp_server(server_id).name == "Renamed"
        await pilot.press("enter")
        await _wait_for_screen(app, ConfirmationScreen, title="CONFIRM DELETE MCP")
        await pilot.press("down", "enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCPs")
        await wait_for_menu_action(pilot, menu)
        assert app.screen is menu
        assert [choice.value for choice in menu._choices] == ["+"]
        assert any("MCP deleted: Renamed" in notice for notice in notices)
        assert any("MCP configuration saved: Renamed" in notice for notice in notices)
        assert all(server_id not in notice for notice in notices)
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert menu.query_one(Input).value == "Manage MCPs"


@pytest.mark.asyncio
async def test_mcp_connect_runs_in_background_and_contains_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = ConfigurationFacade(tmp_path).set_mcp_server_by_name(
        "local",
        MCPServerConfigUpdate(command="unused-in-test", auto_start=False),
    )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    operation_started = asyncio.Event()
    release_operation = asyncio.Event()
    runtime_status = "configured"
    start_calls = 0

    def runtime_servers() -> tuple[McpServerStatus, ...]:
        return (
            McpServerStatus(
                server_id=saved.server_id,
                name="local",
                transport="stdio",
                status=runtime_status,
            ),
        )

    async def failing_start(_server_id: str) -> McpServerStatus:
        nonlocal runtime_status, start_calls
        start_calls += 1
        runtime_status = "starting"
        operation_started.set()
        await release_operation.wait()
        runtime_status = "failed"
        raise RuntimeError("controlled MCP connection failure")

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app._controller, "mcp_servers", runtime_servers)
        monkeypatch.setattr(app._controller, "start_mcp_server", failing_start)
        await _open_mcp_management(app, pilot, "start")
        menu = app.screen
        options = menu.query_one(OptionList)
        options.focus()

        await pilot.press("enter")
        await asyncio.wait_for(operation_started.wait(), timeout=1)

        assert app.screen is menu
        assert next(choice for choice in menu._choices if choice.value == "start").loading
        menu.query_one("#choice-filter", Input).value = "Connect"
        await pilot.pause()
        assert menu.query_one("#choice-filter", Input).value == "Connect"

        release_operation.set()
        deadline = asyncio.get_running_loop().time() + 2
        while next(choice for choice in menu._choices if choice.value == "start").loading:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("MCP failure did not clear the menu loading state")
            await pilot.pause(0.05)

        failed = next(choice for choice in menu._choices if choice.value == "start")
        assert failed.disabled is False
        assert app.screen is menu

        # Refresh disables Connect after an external connection completes.
        runtime_status = "connected"
        await menu.refresh_choices()
        connected = next(choice for choice in menu._choices if choice.value == "start")
        assert connected.disabled is True
        options.highlighted = options.get_option_index("start")
        await pilot.press("enter")
        await pilot.pause()
        assert start_calls == 1


@pytest.mark.asyncio
async def test_mcp_disconnect_runs_in_background_and_refreshes_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = ConfigurationFacade(tmp_path).set_mcp_server_by_name(
        "local",
        MCPServerConfigUpdate(command="unused-in-test", auto_start=False),
    )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    operation_started = asyncio.Event()
    release_operation = asyncio.Event()
    runtime_status = "connected"

    def runtime_servers() -> tuple[McpServerStatus, ...]:
        return (
            McpServerStatus(
                server_id=saved.server_id,
                name="local",
                transport="stdio",
                status=runtime_status,
            ),
        )

    async def controlled_stop(_server_id: str) -> McpServerStatus:
        nonlocal runtime_status
        runtime_status = "stopping"
        operation_started.set()
        await release_operation.wait()
        runtime_status = "stopped"
        return runtime_servers()[0]

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app._controller, "mcp_servers", runtime_servers)
        monkeypatch.setattr(app._controller, "stop_mcp_server", controlled_stop)
        await _open_mcp_management(app, pilot, "stop")
        menu = app.screen
        options = menu.query_one(OptionList)
        options.focus()

        await pilot.press("enter")
        await asyncio.wait_for(operation_started.wait(), timeout=1)
        assert next(choice for choice in menu._choices if choice.value == "stop").loading
        menu.query_one("#choice-filter", Input).value = "Disconnect"
        await pilot.pause()
        assert menu.query_one("#choice-filter", Input).value == "Disconnect"

        release_operation.set()
        deadline = asyncio.get_running_loop().time() + 2
        while next(choice for choice in menu._choices if choice.value == "stop").loading:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("MCP stop did not clear the menu loading state")
            await pilot.pause(0.05)

        stopped = next(choice for choice in menu._choices if choice.value == "stop")
        assert stopped.disabled is True
        assert app.screen is menu
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="MCPs")
        assert (
            next(choice for choice in menu._choices if choice.value == saved.server_id).trailing
            == "stopped"
        )


@pytest.mark.asyncio
async def test_mcp_disconnect_then_connect_survives_closed_action_menus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = ConfigurationFacade(tmp_path).set_mcp_server_by_name(
        "local",
        MCPServerConfigUpdate(command="unused-in-test", auto_start=False),
    )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    runtime_status = "connected"
    stop_started = asyncio.Event()
    stop_release = asyncio.Event()
    stop_finished = asyncio.Event()
    start_started = asyncio.Event()
    start_release = asyncio.Event()
    start_finished = asyncio.Event()

    def runtime_servers() -> tuple[McpServerStatus, ...]:
        return (
            McpServerStatus(
                server_id=saved.server_id,
                name="local",
                transport="stdio",
                status=runtime_status,
                tool_count=1 if runtime_status == "connected" else 0,
            ),
        )

    async def controlled_stop(_server_id: str) -> McpServerStatus:
        nonlocal runtime_status
        runtime_status = "stopping"
        stop_started.set()
        await stop_release.wait()
        runtime_status = "stopped"
        stop_finished.set()
        return runtime_servers()[0]

    async def controlled_start(_server_id: str) -> McpServerStatus:
        nonlocal runtime_status
        runtime_status = "starting"
        start_started.set()
        await start_release.wait()
        runtime_status = "connected"
        start_finished.set()
        return runtime_servers()[0]

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app._controller, "mcp_servers", runtime_servers)
        monkeypatch.setattr(app._controller, "stop_mcp_server", controlled_stop)
        monkeypatch.setattr(app._controller, "start_mcp_server", controlled_start)

        await _open_mcp_management(app, pilot, "stop")
        disconnected_menu = app.screen
        await pilot.press("enter")
        await asyncio.wait_for(stop_started.wait(), timeout=1)
        await pilot.press("escape", "escape", "escape")
        await pilot.pause()
        assert disconnected_menu.is_attached is False

        stop_release.set()
        await asyncio.wait_for(stop_finished.wait(), timeout=1)
        await pilot.pause()
        assert app.is_running is True

        await _open_mcp_management(app, pilot, "start")
        connected_menu = app.screen
        await pilot.press("enter")
        await asyncio.wait_for(start_started.wait(), timeout=1)
        await pilot.press("escape", "escape", "escape")
        await pilot.pause()
        assert connected_menu.is_attached is False

        start_release.set()
        await asyncio.wait_for(start_finished.wait(), timeout=1)
        await pilot.pause()

        assert app.is_running is True
        assert not isinstance(app.screen, ChoiceScreen)
        assert app._system_status.mcp_connected_count == 1


@pytest.mark.asyncio
async def test_tool_permissions_group_all_registered_sources(tmp_path: Path) -> None:
    ConfigurationFacade(tmp_path).set_tool_permission("UnavailableTest", "deny")
    app = OuroTuiApp(home=tmp_path, application=_application_with_tool_sources(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Tool permissions"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="TOOL PERMISSIONS")

        groups = list(dict.fromkeys(choice.group for choice in app.screen._choices))
        assert groups[:3] == [
            "Built-in",
            "MCP",
            "dev.ouro.official",
        ]
        assert groups[-3:] == [
            "dev.ouro.third-party",
            "dev.ouro.generated",
            "Unavailable",
        ]
        choices = {choice.value: choice for choice in app.screen._choices}
        assert choices["BuiltinTest"].group == "Built-in"
        assert choices["McpTest"].group == "MCP"
        assert choices["OfficialPluginTest"].group == "dev.ouro.official"
        assert choices["ThirdPartyPluginTest"].group == "dev.ouro.third-party"
        assert choices["AiGeneratedPluginTest"].group == "dev.ouro.generated"
        assert not any(choice.label == "Plugin default" for choice in choices.values())
        assert choices["UnavailableTest"].group == "Unavailable"
        assert choices["BuiltinTest"].trailing == "Default • Ask for approval"
        assert choices["ContractControlledTest"].trailing == (
            "Plugin controlled • Allow automatically"
        )
        assert choices["ContractControlledTest"].disabled is True
        assert choices["ThirdPartyPluginTest"].disabled is False
        assert choices["UnavailableTest"].trailing == "Block • unavailable"
        assert all(choice.description == "" for choice in choices.values())


@pytest.mark.asyncio
async def test_tool_permission_selection_returns_to_refreshed_tool_menu(tmp_path: Path) -> None:
    app = OuroTuiApp(home=tmp_path, application=_application_with_tool_sources(tmp_path))

    async with run_ready_app(app, size=(100, 34), notifications=True) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await pilot.pause()
        app.screen.query_one("#choice-filter", Input).value = "Tool permissions"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="TOOL PERMISSIONS")

        tool_screen = app.screen
        tool_screen.query_one("#choice-filter", Input).value = "BuiltinTest"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(
            app,
            ChoiceScreen,
            title="TOOL PERMISSION • BuiltinTest",
        )
        assert app.screen is tool_screen
        assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1
        current = next(choice for choice in app.screen._choices if choice.value == "default")
        assert current.label == "Use default"
        assert current.marker == DEFAULT_TUI_THEME.selected_choice_marker
        assert current.trailing == "Ask for approval"
        assert all(choice.trailing != "current" for choice in app.screen._choices)
        app.screen.query_one("#choice-filter", Input).value = "auto"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="TOOL PERMISSIONS")

        assert app.screen is tool_screen
        assert app.screen.query_one("#choice-title", Static).render().plain == ("TOOL PERMISSIONS")
        assert (
            next(choice for choice in app.screen._choices if choice.value == "BuiltinTest").trailing
            == "Allow automatically"
        )
        assert not list(app.screen.query("Toast"))
        assert ConfigurationFacade(tmp_path).permission_config().tools["BuiltinTest"] == "auto"

        await pilot.press("enter")
        await _wait_for_screen(
            app,
            ChoiceScreen,
            title="TOOL PERMISSION • BuiltinTest",
        )
        assert app.screen is tool_screen
        assert sum(isinstance(screen, ChoiceScreen) for screen in app.screen_stack) == 1
        assert "default" in {choice.value for choice in app.screen._choices}
        assert "inherit" not in {choice.value for choice in app.screen._choices}
        app.screen.query_one("#choice-filter", Input).value = "default"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="TOOL PERMISSIONS")
        assert app.screen is tool_screen
        assert (
            next(choice for choice in app.screen._choices if choice.value == "BuiltinTest").trailing
            == "Default • Ask for approval"
        )
        assert not list(app.screen.query("Toast"))
        assert "BuiltinTest" not in ConfigurationFacade(tmp_path).permission_config().tools


@pytest.mark.parametrize(
    ("enabled", "runtime_status", "selectable_runtime_actions"),
    (
        (False, "disabled", set()),
        (True, None, set()),
        (True, "configured", {"start"}),
        (True, "failed", {"start"}),
        (True, "stopped", {"start"}),
        (True, "starting", {"stop"}),
        (True, "reconnecting", {"stop"}),
        (True, "connected", {"refresh", "restart", "stop"}),
        (True, "stopping", set()),
    ),
)
def test_mcp_management_menu_keeps_stable_actions_and_disables_unavailable_ones(
    tmp_path: Path,
    *,
    enabled: bool,
    runtime_status: str | None,
    selectable_runtime_actions: set[str],
) -> None:
    saved = ConfigurationFacade(tmp_path).set_mcp_server_by_name(
        "local",
        MCPServerConfigUpdate(command="python", enabled=enabled, auto_start=False),
    )
    config = saved
    status = (
        McpServerStatus(
            server_id=saved.server_id,
            name="local",
            transport="stdio",
            status=runtime_status,
        )
        if runtime_status is not None
        else None
    )

    choices = _mcp_management_choices(config, status)

    assert [choice.value for choice in choices] == [
        "edit",
        "toggle",
        "check",
        "start",
        "refresh",
        "restart",
        "stop",
        "remove",
    ]
    assert {choice.value for choice in choices if not choice.disabled} == {
        "edit",
        "toggle",
        "check",
        "remove",
        *selectable_runtime_actions,
    }
    assert next(choice for choice in choices if choice.value == "toggle").label == (
        "Disable" if enabled else "Enable"
    )


@pytest.mark.asyncio
async def test_mcp_management_returns_to_catalog_after_deletion(tmp_path: Path) -> None:
    configuration = ConfigurationFacade(tmp_path)
    for name in ("keep", "remove"):
        configuration.set_mcp_server_by_name(
            name,
            MCPServerConfigUpdate(command="python", auto_start=False),
        )
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one("#choice-filter", Input).value = "Manage MCPs"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCPs")
        app.screen.query_one("#choice-filter", Input).value = "remove"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCP • remove")

        options = app.screen.query_one(OptionList)
        options.highlighted = options.get_option_index("remove")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, ConfirmationScreen, title="CONFIRM DELETE MCP")
        await pilot.press("down", "enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCPs")

        assert [choice.label for choice in app.screen._choices] == ["keep", "+"]


@pytest.mark.asyncio
async def test_mcp_runtime_actions_refresh_availability_without_rebuilding_menu(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    saved = configuration.set_mcp_server_by_name(
        "local",
        MCPServerConfigUpdate(
            command="unused-in-test",
            auto_start=False,
            auto_reconnect=False,
            health_check_interval_seconds=60,
        ),
    )
    application = _application(tmp_path)
    session = LiveMcpSession()
    manager = MCPManager(
        registry=application.runtime.tools,
        client_factory=LiveMcpFactory(session),
    )
    config = configuration.mcp.get(saved.server_id)
    assert config is not None
    manager.configure(config)
    application.mcp = MCPRuntimeResource(manager)
    app = OuroTuiApp(home=tmp_path, application=application)

    async with (
        run_ready_app(app, size=(100, 34)) as pilot,
        release_on_exit(session.initialize_gate),
    ):
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        action_screen = app.screen
        action_screen.query_one("#choice-filter", Input).value = "Manage MCPs"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCPs")
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCP • local")

        options = action_screen.query_one("#choice-list", OptionList)
        assert options.get_option("start").disabled is False
        options.highlighted = options.get_option_index("start")
        options.focus()
        await pilot.press("enter")

        assert action_screen is app.screen
        await _wait_until(
            lambda: (
                next(choice for choice in action_screen._choices if choice.value == "start").loading
            ),
            pilot,
            description="MCP connection held at initialization",
        )
        action_screen.query_one("#choice-filter", Input).value = "runtime"
        await pilot.pause()
        assert action_screen.query_one("#choice-filter", Input).value == "runtime"

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="MCPs")
        assert (
            next(
                choice for choice in action_screen._choices if choice.value == saved.server_id
            ).loading
            is True
        )

        session.initialize_gate.set()
        await _wait_until(
            lambda: (
                not next(
                    choice for choice in action_screen._choices if choice.value == saved.server_id
                ).loading
            ),
            pilot,
            description="MCP start reflected in its parent menu",
        )
        assert (
            next(
                choice for choice in action_screen._choices if choice.value == saved.server_id
            ).trailing
            == "connected"
        )

        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="MCP • local")
        options = action_screen.query_one("#choice-list", OptionList)

        assert action_screen is app.screen
        assert options.get_option("start").disabled is True
        assert options.get_option("refresh").disabled is False
        assert options.get_option("restart").disabled is False
        assert options.get_option("stop").disabled is False

        options.highlighted = options.get_option_index("stop")
        await pilot.press("enter")
        await _wait_until(
            lambda: options.get_option("stop").disabled,
            pilot,
            description="MCP stop reflected in its menu",
        )

        assert [choice.value for choice in action_screen._choices] == [
            "edit",
            "toggle",
            "check",
            "start",
            "refresh",
            "restart",
            "stop",
            "remove",
        ]
        assert options.get_option("start").disabled is False
        assert options.get_option("refresh").disabled is True
        assert options.get_option("restart").disabled is True
        assert options.get_option("stop").disabled is True

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="MCPs")
        assert (
            next(
                choice for choice in action_screen._choices if choice.value == saved.server_id
            ).trailing
            == "stopped"
        )
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        assert any(choice.value.endswith(":mcp.list") for choice in action_screen._choices)


@pytest.mark.asyncio
async def test_mcp_configuration_screen_validates_json_and_returns_typed_draft(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    results = []

    async with run_ready_app(app, size=(100, 40)) as pilot:
        await pilot.pause()
        screen = MCPConfigurationScreen()
        app.push_screen(screen, callback=results.append)
        await pilot.pause()
        assert isinstance(screen, EditableChoiceScreen)
        assert all(choice.label != "Server ID" for choice in screen._choices)
        assert not list(screen.query("Button"))
        assert not list(screen.query("Checkbox"))
        assert not list(screen.query("Select"))
        assert {choice.group for choice in screen._choices} == {
            "DETAILS",
            "CONNECTION",
            "PROCESS",
            "CREDENTIALS",
            "TOOLS",
            "CONNECTION SETTINGS",
            "ACTIONS",
        }
        target = next(choice for choice in screen._choices if choice.value == "mcp-target")
        assert target.label == "Command"
        assert target.edit_placeholder == (
            "Enter executable only (e.g. npx); put arguments in Arguments (JSON)"
        )
        screen._values["mcp-transport"] = "http"
        screen._refresh("mcp-target")
        target = next(choice for choice in screen._choices if choice.value == "mcp-target")
        assert target.label == "URL"
        assert target.edit_placeholder == "Enter the MCP URL"
        screen._values["mcp-transport"] = "stdio"
        screen._refresh("mcp-target")
        options = screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("mcp-name")
        options.focus()
        await pilot.press("enter")
        await pilot.pause()
        prompt = options.get_option("mcp-name").prompt
        assert isinstance(prompt, Text)
        assert "Enter a display name" in prompt.plain
        placeholder_offset = prompt.plain.index("Enter a display name")
        assert prompt.get_style_at_offset(Console(), placeholder_offset).bold is False
        await pilot.press("escape")
        await pilot.pause()

        options.highlighted = options.get_option_index("mcp-name")
        options.focus()
        await pilot.press("enter", *"local")
        await pilot.pause()
        prompt = options.get_option("mcp-name").prompt
        assert isinstance(prompt, Text)
        assert prompt.get_style_at_offset(Console(), prompt.plain.index("local")).bold is False
        await pilot.press("escape")
        await pilot.pause()

        screen._values.update(
            {
                "mcp-name": "Local server",
                "mcp-target": "python",
                "mcp-args": "not-json",
            }
        )
        screen._refresh("mcp-save")
        save = next(choice for choice in screen._choices if choice.value == "mcp-save")
        assert save.disabled is True
        assert "valid JSON" in save.trailing

        screen._values["mcp-args"] = '["-m", "server"]'
        screen._values["mcp-tool-effects"] = '{"read":"read_only"}'
        screen._refresh("mcp-save")
        await pilot.press("enter")
        await pilot.pause()

        assert len(results) == 1
        draft = results[0]
        assert draft is not None
        assert draft.server_id is None
        assert draft.name == "Local server"
        assert draft.transport == "stdio"
        assert draft.target == "python"
        assert draft.args == ["-m", "server"]
        assert draft.tool_effect_overrides == {"read": "read_only"}
