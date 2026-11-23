"""Structural conversions shared by saved configuration and frozen agent records."""

from __future__ import annotations

from typing import Any

from ouro_upgrade.errors import require


def upgrade_definition(definition: dict[str, Any]) -> None:
    definition.pop("can_spawn", None)
    definition.pop("contract_hash", None)


def upgrade_spawn_policy(policy: dict[str, Any]) -> None:
    if "allowed_lifecycle_bindings" in policy:
        require(
            policy["allowed_lifecycle_bindings"] == ["bound"],
            "agent policy",
            "unsupported agent lifecycle",
        )
        policy.pop("allowed_lifecycle_bindings")
