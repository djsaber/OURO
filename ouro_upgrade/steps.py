"""Deterministic, forward-only transitions between explicitly supported schemas."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ouro_upgrade.errors import UpgradeDataError, UpgradeError


@dataclass(frozen=True)
class UpgradeStep[T]:
    target: int
    transform: Callable[[T], None]


def plan[T](
    kind: str,
    version: object,
    *,
    target: int,
    steps: Mapping[int, UpgradeStep[T]],
) -> tuple[UpgradeStep[T], ...]:
    """Resolve the complete registered path before any transformation runs."""
    if type(version) is not int or version > target or (version != target and version not in steps):
        raise UpgradeDataError(f"unsupported {kind} version; expected {(*steps, target)}")
    result = []
    while version != target:
        step = steps.get(version)
        if step is None:
            raise UpgradeError(f"missing {kind} upgrade step from version {version}")
        if type(step.target) is not int or not version < step.target <= target:
            raise UpgradeError(f"invalid {kind} upgrade step from version {version}")
        result.append(step)
        version = step.target
    return tuple(result)


def advance(
    kind: str,
    value: dict[str, Any],
    *,
    target: int,
    steps: Mapping[int, UpgradeStep[dict[str, Any]]],
) -> None:
    """Advance an exclusively owned record, including records within a copied envelope."""
    for step in plan(kind, value.get("schema_version"), target=target, steps=steps):
        step.transform(value)
        value["schema_version"] = step.target
