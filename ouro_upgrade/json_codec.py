"""Strict JSON encoding shared by records and recovery metadata."""

from __future__ import annotations

import json
import math
from typing import Any

from ouro_upgrade.errors import UpgradeDataError


def loads(text: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise UpgradeDataError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=_invalid_constant,
            parse_float=_finite_float,
        )
    except (ValueError, RecursionError) as exc:
        raise UpgradeDataError("invalid JSON object") from exc
    if not isinstance(value, dict):
        raise UpgradeDataError("expected JSON object")
    return value


def _invalid_constant(_: str) -> None:
    raise UpgradeDataError("non-finite JSON number")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise UpgradeDataError("non-finite JSON number")
    return value


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
