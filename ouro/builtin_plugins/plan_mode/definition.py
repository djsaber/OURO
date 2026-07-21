from __future__ import annotations

from pathlib import Path

from ouro.plugins.builtin import BuiltinPluginDefinition
from ouro.plugins.manifest import PluginManifest

from .plan_mode_plugin import create_plugin

PLAN_MODE_PLUGIN = BuiltinPluginDefinition(
    manifest=PluginManifest.model_validate(
        {
            "schema_version": 2,
            "plugin_id": "dev.ouro.plan_mode",
            "name": "Plan Mode",
            "version": "1.0.0",
            "description": "Implements plan mode as composable OURO harness contributions.",
            "namespace": "plan_mode",
            "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^1.0.0"},
            "runtime": {
                "kind": "python-inprocess",
                "entrypoint": "plan_mode_plugin:create_plugin",
            },
            "contributes": {
                "tools": [
                    {
                        "id": "enter",
                        "exposed_name": "EnterPlanMode",
                        "description": (
                            "Use this tool proactively before starting a non-trivial "
                            "implementation task."
                        ),
                        "permission_mode": "auto",
                        "permission_control": "tool",
                        "effect": "state_changing",
                        "batch_behavior": "exclusive",
                    },
                    {
                        "id": "exit",
                        "exposed_name": "ExitPlanMode",
                        "description": "Submit the current plan file for user approval.",
                        "permission_mode": "auto",
                        "permission_control": "tool",
                        "effect": "state_changing",
                        "batch_behavior": "exclusive",
                        "resumable": True,
                        "supports_unattended": True,
                        "active_when": "mode",
                    },
                ],
                "commands": [
                    {"id": "plan", "description": "Enter plan mode for the current Session."}
                ],
                "session_controllers": [
                    {
                        "id": "mode",
                        "description": "Own the Plan mode lifecycle for one Session.",
                        "claims": [
                            {
                                "domain": "ouro.session.primary_behavior",
                                "access": "exclusive",
                            }
                        ],
                        "timeout_seconds": 5,
                    }
                ],
                "context_providers": [
                    {
                        "id": "plan-scaffold",
                        "priority": 10,
                        "timeout_seconds": 5,
                        "failure_mode": "block",
                        "active_when": "mode",
                    }
                ],
                "tool_policies": [
                    {
                        "id": "plan-tool-gate",
                        "priority": 10,
                        "timeout_seconds": 5,
                        "active_when": "mode",
                    }
                ],
            },
            "permissions": [],
            "dependencies": {"plugins": []},
            "metadata": {},
        }
    ),
    factory=create_plugin,
    root=Path(__file__).parent,
    default_enabled=False,
)


__all__ = ["PLAN_MODE_PLUGIN"]
