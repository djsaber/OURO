from __future__ import annotations

from pathlib import Path

from ouro.plugins.builtin import BuiltinPluginDefinition
from ouro.plugins.manifest import PluginManifest

from .agent_context_plugin import create_plugin

AGENT_CONTEXT_PLUGIN = BuiltinPluginDefinition(
    manifest=PluginManifest.model_validate(
        {
            "schema_version": 3,
            "plugin_id": "dev.ouro.agent_context",
            "name": "Agent Context",
            "version": "1.0.0",
            "description": (
                "Injects hierarchical AGENTS.md instructions and request-time runtime facts "
                "into the complete Agent system prompt."
            ),
            "namespace": "agent_context",
            "engines": {"ouro": ">=0.4.0", "plugin_api": "^5.0.0"},
            "runtime": {
                "kind": "python-inprocess",
                "entrypoint": "agent_context_plugin:create_plugin",
            },
            "contributes": {
                "context_providers": [
                    {
                        "id": "agent-instructions",
                        "priority": 9_000,
                        "timeout_seconds": 5,
                        "failure_mode": "omit",
                    },
                    {
                        "id": "runtime-facts",
                        "priority": 10_000,
                        "timeout_seconds": 5,
                        "failure_mode": "omit",
                    },
                ]
            },
            "permissions": [],
            "dependencies": {"plugins": []},
            "metadata": {},
        }
    ),
    factory=create_plugin,
    root=Path(__file__).parent,
    default_enabled=True,
)

__all__ = ["AGENT_CONTEXT_PLUGIN"]
