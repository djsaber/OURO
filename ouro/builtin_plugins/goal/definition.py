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
            "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^2.0.0"},
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
                    },
                ],
                "commands": [
                    {
                        "id": "goal",
                        "description": (
                            "Create and control the latest Goal for this conversation."
                        ),
                        "usage": (
                            "[--token-budget N] <objective> | status | pause | "
                            "resume [--token-budget N] | cancel | "
                            "replace [--token-budget N] <objective>"
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
                                "usage": "[--token-budget N] <objective>",
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
                        "id": "goal-run-binding",
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
                "prompt_policies": [
                    {
                        "id": "goal-input-gate",
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
                {
                    "id": "sessions.prompt.inspect",
                    "reason": "Prevent ordinary prompts from implicitly resuming a stopped Goal.",
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
