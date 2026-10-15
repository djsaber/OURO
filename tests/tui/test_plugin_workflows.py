from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from textual.widgets import Input, OptionList

from ouro.application.plugin_models import PluginDetail, PluginInstallView, PluginProvenanceView
from ouro.tui.app import OuroTuiApp
from ouro.tui.plugin_workflows import _plugin_action_choices
from ouro.tui.screens import Choice, ChoiceScreen, ConfirmationScreen
from ouro.tui.theme import DEFAULT_TUI_THEME
from tests.tui.helpers import run_ready_app
from tests.tui.test_app import _application, _wait_for_screen


def _detail() -> PluginDetail:
    return PluginDetail(
        plugin_id="dev.ouro.example",
        name="Example",
        version="1.0.0",
        package_digest="a" * 64,
        status="disabled",
        runtime_kind="worker",
        desired_enabled=False,
        plugin_class="user",
        namespace="example",
    )


def test_plugin_actions_use_explicit_lifecycle_without_duplicate_trust_actions() -> None:
    actions = {choice.value for choice in _plugin_action_choices(_detail())}

    assert {"enable", "check", "remove"} <= actions
    assert not {"authorize", "trust-code", "revoke", "revoke-code-trust"} & actions


def test_plugin_management_uses_one_action_group_and_the_theme_danger_color() -> None:
    plugin = _detail()
    plugin.configuration_supported = True
    plugin.installs = [
        PluginInstallView(
            **plugin.model_dump(include={"plugin_id", "name", "runtime_kind", "plugin_class"}),
            version=version,
            package_digest=digest * 64,
            provenance=PluginProvenanceView(source_kind="local", public_source="example"),
        )
        for version, digest in (("1.0.0", "a"), ("2.0.0", "b"))
    ]
    theme = replace(DEFAULT_TUI_THEME, danger="#ff1234")

    choices = _plugin_action_choices(plugin, theme=theme)

    assert {choice.group for choice in choices} == {"ACTIONS"}
    actions = {choice.value: choice for choice in choices}
    assert actions["check"].label == "Check"
    assert actions["configure"].label == "Settings"
    assert actions["remove"].label == "Delete"
    assert actions["remove"].accent == theme.danger
    assert actions["remove"].destructive
    assert list(actions) == ["configure", "select-install", "enable", "check", "restart", "remove"]


@pytest.mark.parametrize("leave", ("stay", "back", "close"))
@pytest.mark.parametrize("failure", (False, True))
async def test_plugin_delete_animates_and_does_not_navigate_after_leaving(
    tmp_path, monkeypatch, leave, failure
):
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def plugin(_plugin_id):
        return _detail()

    async def plugins():
        return ()

    async def remove(plugin_id):
        calls.append(plugin_id)
        entered.set()
        await release.wait()
        if failure:
            raise OSError("Plugin removal failed")

    async with run_ready_app(app) as pilot:
        monkeypatch.setattr(app._controller, "plugin", plugin)
        monkeypatch.setattr(app._controller, "plugins", plugins)
        monkeypatch.setattr(app._controller, "remove_plugin", remove)
        menu = ChoiceScreen(title="ROOT", placeholder="Filter…", choices=(Choice("root", "Root"),))
        await app.push_screen(menu)
        menu.push_page(
            title="PLUGINS", placeholder="Filter…", choices=(Choice("plugin", "Example"),)
        )
        await app._manage_plugin(_detail().plugin_id, menu=menu)
        options = menu.query_one(OptionList)
        options.highlighted = options.get_option_index("remove")
        await pilot.press("enter")
        await _wait_for_screen(app, ConfirmationScreen, title="CONFIRM DELETE PLUGIN")
        await pilot.press("down", "enter")
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            await pilot.pause()
            assert app.screen is menu
            assert options.get_option("remove").prompt.plain.startswith(
                app.tui_theme.spinner_frames
            )
            await pilot.press("enter")
            assert calls == [_detail().plugin_id]
            if leave != "stay":
                await pilot.press("escape")
                assert menu._title == "PLUGINS"
                if leave == "close":
                    await pilot.press("escape", "escape")
                    assert app.screen is not menu
                else:
                    await pilot.press(*"Example")
        finally:
            release.set()
        await app.workers.wait_for_complete(
            [worker for worker in app.workers if worker.group.startswith("choice-")]
        )
        await pilot.pause()
        if leave == "close":
            assert app.screen is not menu
        else:
            expected_title = "PLUGIN • Example" if failure and leave == "stay" else "PLUGINS"
            assert app.screen is menu and menu._title == expected_title
            assert menu.query_one(Input).value == ("Example" if leave == "back" else "")
            assert menu._spinner_timer is None


async def test_standalone_plugin_delete_closes_the_removed_catalog(tmp_path, monkeypatch):
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))

    async def plugin(_plugin_id):
        return _detail()

    async def plugins():
        return (_detail(),)

    async def remove(_plugin_id):
        return None

    async with run_ready_app(app) as pilot:
        monkeypatch.setattr(app._controller, "plugin", plugin)
        monkeypatch.setattr(app._controller, "plugins", plugins)
        monkeypatch.setattr(app._controller, "remove_plugin", remove)
        worker = app.run_worker(app._choose_plugin_action("remove"))
        await _wait_for_screen(app, ChoiceScreen, title="DELETE PLUGIN")
        menu = app.screen
        await pilot.press("enter")
        await _wait_for_screen(app, ConfirmationScreen, title="CONFIRM DELETE PLUGIN")
        await pilot.press("down", "enter")
        async with asyncio.timeout(2):
            while app.screen is menu:
                await pilot.pause(0.01)
        await worker.wait()
