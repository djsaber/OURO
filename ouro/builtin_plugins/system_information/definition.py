from __future__ import annotations

from pathlib import Path

from ouro.plugins.builtin import BuiltinPluginDefinition
from ouro.plugins.manifest import PluginManifest

from .system_information_plugin import create_plugin

SYSTEM_INFORMATION_PLUGIN = BuiltinPluginDefinition(
    manifest=PluginManifest.model_validate(
        {
            "schema_version": 3,
            "plugin_id": "dev.ouro.system_information",
            "name": "System Information",
            "version": "1.0.0",
            "description": (
                "Injects request-time system and Session environment facts at the end "
                "of the complete Agent system prompt."
            ),
            "namespace": "system_information",
            "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^2.0.0"},
            "runtime": {
                "kind": "python-inprocess",
                "entrypoint": "system_information_plugin:create_plugin",
            },
            "contributes": {
                "context_providers": [
                    {
                        "id": "runtime-facts",
                        "priority": 10_000,
                        "timeout_seconds": 5,
                        "failure_mode": "omit",
                    }
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

__all__ = ["SYSTEM_INFORMATION_PLUGIN"]
