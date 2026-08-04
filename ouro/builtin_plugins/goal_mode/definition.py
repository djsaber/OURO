from __future__ import annotations

from pathlib import Path

from ouro.plugins.builtin import BuiltinPluginDefinition
from ouro.plugins.manifest import PluginManifest

from .goal_mode_plugin import create_plugin
from .prompts import (
    CREATE_GOAL_DESCRIPTION,
    GET_GOAL_DESCRIPTION,
    UPDATE_GOAL_DESCRIPTION,
)

GOAL_MODE_PLUGIN = BuiltinPluginDefinition(
    manifest=PluginManifest.model_validate(
        {
            "schema_version": 3,
            "plugin_id": "dev.ouro.goal_mode",
            "name": "Goal Mode",
            "version": "1.0.0",
            "description": "Runs one durable, user-owned Goal across bounded OURO Runs.",
            "namespace": "goal_mode",
            "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^2.0.0"},
            "runtime": {
                "kind": "python-inprocess",
                "entrypoint": "goal_mode_plugin:create_plugin",
            },
            "contributes": {
                "tools": [
                    {
                        "id": "get-goal",
                        "exposed_name": "GetGoal",
                        "description": GET_GOAL_DESCRIPTION,
                        "permission_mode": "auto",
                        "permission_control": "tool",
                        "effect": "read_only",
                    },
                    {
                        "id": "create-goal",
                        "exposed_name": "CreateGoal",
                        "description": CREATE_GOAL_DESCRIPTION,
                        "permission_mode": "approval",
                        "permission_control": "host",
                        "effect": "state_changing",
                        "batch_behavior": "exclusive",
                    },
                    {
                        "id": "update-goal",
                        "exposed_name": "UpdateGoal",
                        "description": UPDATE_GOAL_DESCRIPTION,
                        "permission_mode": "auto",
                        "permission_control": "tool",
                        "effect": "state_changing",
                        "batch_behavior": "exclusive",
                        "active_when": "mode",
                    },
                ],
                "commands": [
                    {
                        "id": "goal",
                        "description": (
                            "Create and control a durable Goal for the current Session."
                        ),
                        "usage": (
                            "[--token-budget N] <request> | status | pause | "
                            "resume [--token-budget N] | cancel | "
                            "replace [--token-budget N] <request>"
                        ),
                        "argument_options": [
                            {
                                "value": "status",
                                "description": "Show the current Goal.",
                            },
                            {
                                "value": "pause",
                                "description": "Pause the active Goal.",
                            },
                            {
                                "value": "resume",
                                "description": "Resume a paused or blocked Goal.",
                                "usage": "[--token-budget N]",
                            },
                            {
                                "value": "cancel",
                                "description": "Cancel and remove the current Goal.",
                            },
                            {
                                "value": "replace",
                                "description": "Replace the current Goal with a new objective.",
                                "usage": "[--token-budget N] <request>",
                            },
                        ],
                        "requires_session": True,
                    }
                ],
                "session_controllers": [
                    {
                        "id": "mode",
                        "description": "Own one durable Goal lifecycle for a primary Session.",
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
                        "id": "goal-scaffold",
                        "priority": 10,
                        "timeout_seconds": 5,
                        "failure_mode": "block",
                        "active_when": "mode",
                    }
                ],
                "continuation_policies": [
                    {
                        "id": "goal-continuation",
                        "priority": 10,
                        "timeout_seconds": 5,
                        "active_when": "mode",
                    }
                ],
            },
            "permissions": [
                {
                    "id": "sessions.write",
                    "reason": (
                        "Persist Goal state and submit attributed Goal continuation inputs."
                    ),
                },
                {
                    "id": "background.run",
                    "reason": "Continue an active Goal after each bounded Run settles.",
                },
            ],
            "dependencies": {"plugins": []},
            "metadata": {},
        }
    ),
    factory=create_plugin,
    root=Path(__file__).parent,
    default_enabled=False,
)

__all__ = ["GOAL_MODE_PLUGIN"]
