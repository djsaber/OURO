from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Input, OptionList, Static

from ouro.application.contracts.plugins import (
    PluginActionResult,
    PluginDetail,
    PluginInstallView,
    PluginProvenanceView,
)
from ouro.plugins.manifest import PluginAuthor
from ouro.tui.app import OuroTuiApp
from ouro.tui.features.plugins.workflows import (
    _plugin_action_choices,
    _plugin_choices,
    _plugin_description,
)
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.confirmation import ConfirmationScreen
from ouro.tui.menus.models import Choice
from tests.tui.app.support import _application
from tests.tui.helpers import run_ready_app, wait_for_workers
from tests.tui.helpers import wait_for_screen as _wait_for_screen


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


@pytest.mark.parametrize("author", (None, PluginAuthor(name="Example team")))
def test_plugin_author_is_only_shown_in_details(author) -> None:
    plugin = _detail()
    plugin.author = author
    plugin.description = "Example plugin."

    (choice,) = _plugin_choices((plugin,))

    assert choice.trailing == "v1.0.0"
    assert choice.description == "dev.ouro.example"
    assert _plugin_description(plugin) == (
        "Example plugin.\n\nv1.0.0 • By Example team" if author else "Example plugin.\n\nv1.0.0"
    )
    assert all(choice.group != "DETAILS" for choice in _plugin_action_choices(plugin))


def test_plugin_actions_use_explicit_lifecycle_without_duplicate_trust_actions() -> None:
    actions = {choice.value for choice in _plugin_action_choices(_detail())}

    assert {"enable", "remove"} <= actions
    assert not {"check", "authorize", "trust-code", "revoke", "revoke-code-trust"} & actions


def test_plugin_management_groups_lifecycle_actions_and_marks_delete_destructive() -> None:
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
    choices = _plugin_action_choices(plugin)

    assert {choice.group for choice in choices} == {"ACTIONS"}
    actions = {choice.value: choice for choice in choices if choice.group == "ACTIONS"}
    assert actions["configure"].label == "Settings"
    assert actions["remove"].label == "Delete"
    assert actions["remove"].destructive
    assert list(actions) == ["configure", "select-install", "enable", "restart", "remove"]


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
        await app._plugins._manage_plugin(_detail().plugin_id, menu=menu)
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
        await wait_for_workers(
            app, [worker for worker in app.workers if worker.group.startswith("choice-")]
        )
        await pilot.pause()
        if leave == "close":
            assert app.screen is not menu
        else:
            expected_title = "PLUGIN • Example" if failure and leave == "stay" else "PLUGINS"
            assert app.screen is menu and menu._title == expected_title
            assert menu.query_one(Input).value == ("Example" if leave == "back" else "")
            assert menu._spinner_timer is None


async def test_plugin_description_updates_after_switching_version(tmp_path, monkeypatch):
    app = OuroTuiApp(home=tmp_path, application=_application(tmp_path))
    detail = _detail()
    detail.description = "Example plugin."
    detail.author = PluginAuthor(name="Example team")
    detail.installs = [
        PluginInstallView(
            **detail.model_dump(include={"plugin_id", "name", "runtime_kind", "plugin_class"}),
            version=version,
            package_digest=digest * 64,
            selected=digest == "a",
            provenance=PluginProvenanceView(source_kind="local", public_source="example"),
        )
        for version, digest in (("1.0.0", "a"), ("2.0.0", "b"))
    ]

    async def plugin(_plugin_id):
        return detail

    async def select_install(plugin_id, package_digest):
        detail.version = "2.0.0"
        detail.author = None
        return PluginActionResult(
            action="select", plugin_id=plugin_id, status="disabled", package_digest=package_digest
        )

    async with run_ready_app(app) as pilot:
        monkeypatch.setattr(app._controller, "plugin", plugin)
        monkeypatch.setattr(app._controller, "select_plugin_install", select_install)
        menu = ChoiceScreen(title="PLUGINS", placeholder="Filter…", choices=())
        await app.push_screen(menu)
        await app._plugins._manage_plugin(detail.plugin_id, menu=menu)
        await app._plugins._choose_plugin_install(detail.plugin_id, menu=menu)
        options = menu.query_one(OptionList)
        options.highlighted = options.get_option_index("b" * 64)
        options.focus()
        await pilot.press("enter")
        await wait_for_workers(
            app, [worker for worker in app.workers if worker.group.startswith("choice-")]
        )
        await pilot.press("escape")
        await pilot.pause()
        assert menu._title == "PLUGIN • Example"
        assert str(menu.query_one("#choice-description", Static).content) == (
            "Example plugin.\n\nv2.0.0"
        )
