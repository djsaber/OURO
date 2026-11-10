"""Structural conversions shared by saved configuration and frozen agent records."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from ouro.core.agents import AgentDefinition
from ouro_upgrade.errors import UpgradeDataError, require


def upgrade_definition(definition: dict[str, Any]) -> None:
    if "can_spawn" not in definition:
        return
    can_spawn = definition["can_spawn"]
    require(type(can_spawn) is bool, "agent definition", "invalid spawn capability")
    source = {key: item for key, item in definition.items() if key != "contract_hash"}
    encoded = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    expected = "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    require(
        not definition.get("contract_hash") or definition["contract_hash"] == expected,
        "agent definition",
        "agent definition contract hash does not match its content",
    )
    source.pop("can_spawn")
    try:
        parsed = AgentDefinition.model_validate(source)
    except ValidationError:
        raise UpgradeDataError("invalid agent definition") from None
    definition.pop("can_spawn")
    definition["contract_hash"] = parsed.contract_hash


def upgrade_spawn_policy(policy: dict[str, Any]) -> None:
    if "allowed_lifecycle_bindings" in policy:
        require(
            policy["allowed_lifecycle_bindings"] == ["bound"],
            "agent policy",
            "unsupported agent lifecycle",
        )
        policy.pop("allowed_lifecycle_bindings")
