from __future__ import annotations

from ouro.sdk.plugins.v3 import PluginContext


def test_plugin_instance_context_exposes_no_session_writer() -> None:
    assert "session_state" not in PluginContext.__dataclass_fields__
