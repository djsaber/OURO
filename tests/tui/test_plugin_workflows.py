from __future__ import annotations

from dataclasses import replace

from ouro.application.plugin_models import PluginDetail, PluginInstallView, PluginProvenanceView
from ouro.tui.plugin_workflows import _plugin_action_choices
from ouro.tui.theme import DEFAULT_TUI_THEME


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
