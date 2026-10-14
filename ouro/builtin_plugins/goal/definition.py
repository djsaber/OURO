from __future__ import annotations

from pathlib import Path

from ouro.plugins.builtin import BuiltinPluginDefinition
from ouro.plugins.manifest import PluginManifest

from .goal_plugin import create_plugin
from .prompts import (
    CREATE_GOAL_DESCRIPTION,
    GET_GOAL_DESCRIPTION,
    UPDATE_GOAL_DESCRIPTION,
)

GOAL_PLUGIN = BuiltinPluginDefinition(
    manifest=PluginManifest.model_validate(
        {
            "schema_version": 3,
            "plugin_id": "dev.ouro.goal",
            "name": "Goal",
            "version": "2.0.0",
            "description": "Maintains and advances the latest user-owned Goal.",
            "namespace": "goal",
            "engines": {"ouro": ">=0.3.0,<0.4.0", "plugin_api": "^3.0.0"},
            "runtime": {
                "kind": "python-inprocess",
                "entrypoint": "goal_plugin:create_plugin",
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
                        "permission_mode": "auto",
                        "permission_control": "tool",
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
                    },
                ],
                "commands": [
                    {
                        "id": "goal",
                        "description": (
                            "Create and control the latest Goal for this conversation."
                        ),
                        "usage": (
                            "[--token-budget N | --no-token-budget] <objective> | status | pause | "
                            "resume [--token-budget N | --no-token-budget] | cancel | "
                            "replace [--token-budget N | --no-token-budget] <objective>"
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
                                "description": "Resume a stopped Goal, preserving its budget.",
                                "usage": "[--token-budget N | --no-token-budget]",
                            },
                            {
                                "value": "cancel",
                                "description": "Cancel and remove the current Goal.",
                            },
                            {
                                "value": "replace",
                                "description": (
                                    "Edit the current objective, preserving usage and budget."
                                ),
                                "usage": "[--token-budget N | --no-token-budget] <objective>",
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
                "continuation_policies": [
                    {
                        "id": "goal-continuation",
                        "priority": 10,
                        "timeout_seconds": 5,
                        "active_when": "mode",
                    }
                ],
                "context_providers": [
                    {
                        "id": "goal-context",
                        "priority": 10,
                        "timeout_seconds": 5,
                        "failure_mode": "block",
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
    default_enabled=True,
)

__all__ = ["GOAL_PLUGIN"]
