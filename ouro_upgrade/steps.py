"""Deterministic, forward-only transitions between explicitly supported schemas."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ouro_upgrade.errors import UpgradeDataError, UpgradeError


@dataclass(frozen=True)
class UpgradeStep:
    target: int
    transform: Callable[[dict[str, Any]], None]


def migrate(
    kind: str,
    raw: dict[str, Any],
    *,
    target: int,
    steps: Mapping[int, UpgradeStep],
) -> dict[str, Any]:
    """Copy the input and follow one registered transition per source version."""
    value = copy.deepcopy(raw)
    advance(kind, value, target=target, steps=steps)
    return value


def advance(
    kind: str,
    value: dict[str, Any],
    *,
    target: int,
    steps: Mapping[int, UpgradeStep],
) -> None:
    """Advance an exclusively owned record, including records within a copied envelope."""
    version = value.get("schema_version")
    if type(version) is not int or version > target or (version != target and version not in steps):
        raise UpgradeDataError(f"unsupported {kind} version; expected {(*steps, target)}")
    while version != target:
        step = steps.get(version)
        if step is None:
            raise UpgradeError(f"missing {kind} upgrade step from version {version}")
        if type(step.target) is not int or not version < step.target <= target:
            raise UpgradeError(f"invalid {kind} upgrade step from version {version}")
        step.transform(value)
        version = step.target
        value["schema_version"] = version
