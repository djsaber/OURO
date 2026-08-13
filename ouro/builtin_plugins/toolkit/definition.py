from __future__ import annotations

from pathlib import Path

from ouro.plugins.builtin import BuiltinPluginDefinition
from ouro.plugins.manifest import PluginManifest

from .toolkit_plugin import (
    GLOB_DESCRIPTION,
    GREP_DESCRIPTION,
    TODOLIST_DESCRIPTION,
    create_plugin,
)

TOOLKIT_PLUGIN = BuiltinPluginDefinition(
    manifest=PluginManifest.model_validate(
        {
            "schema_version": 3,
            "plugin_id": "dev.ouro.toolkit",
            "name": "Toolkit",
            "version": "1.0.0",
            "description": (
                "Extended agent tools: Glob file search, Grep content search, and a "
                "Session-scoped TodoList."
            ),
            "namespace": "toolkit",
            "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^2.0.0"},
            "runtime": {
                "kind": "python-inprocess",
                "entrypoint": "toolkit_plugin:create_plugin",
            },
            "contributes": {
                "tools": [
                    {
                        "id": "glob",
                        "exposed_name": "Glob",
                        "description": GLOB_DESCRIPTION,
                        "permission_mode": "auto",
                        "effect": "read_only",
                    },
                    {
                        "id": "grep",
                        "exposed_name": "Grep",
                        "description": GREP_DESCRIPTION,
                        "permission_mode": "auto",
                        "effect": "read_only",
                    },
                    {
                        "id": "todolist",
                        "exposed_name": "TodoList",
                        "description": TODOLIST_DESCRIPTION,
                        "permission_mode": "auto",
                        "effect": "state_changing",
                    },
                ],
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


__all__ = ["TOOLKIT_PLUGIN"]
