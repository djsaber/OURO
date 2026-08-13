from __future__ import annotations

from ouro.application.plugin_models import PluginDetail, PluginTrustView
from ouro.plugins.models import PluginTrustStatus
from ouro.tui.plugin_workflows import _plugin_action_choices


def _detail(*, capability: PluginTrustStatus, code: PluginTrustStatus) -> PluginDetail:
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
        trust=PluginTrustView(
            capability_authorization=capability,
            code_trust=code,
        ),
    )


def test_plugin_actions_follow_host_projected_trust_state() -> None:
    pending = {choice.value for choice in _plugin_action_choices(
        _detail(capability="missing", code="missing")
    )}
    assert {"authorize", "trust-code"} <= pending
    assert "revoke" not in pending
    assert "revoke-code-trust" not in pending

    granted = {choice.value for choice in _plugin_action_choices(
        _detail(capability="granted", code="granted")
    )}
    assert {"revoke", "revoke-code-trust"} <= granted
    assert "authorize" not in granted
    assert "trust-code" not in granted

    not_required = {choice.value for choice in _plugin_action_choices(
        _detail(capability="not_required", code="not_required")
    )}
    assert not {"authorize", "trust-code", "revoke", "revoke-code-trust"} & not_required
