from __future__ import annotations

from ouro.application.plugin_models import PluginDetail
from ouro.tui.plugin_workflows import _plugin_action_choices


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
