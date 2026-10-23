"""Parse and present values shared by configuration screens."""

from __future__ import annotations

import json
from collections.abc import Mapping

from ouro.core.reasoning import ReasoningRequest

_RUNTIME_MODEL = "__ouro_runtime_model__"


_NO_FALLBACK = "__ouro_no_fallback__"


_REASONING_REQUESTS: dict[str, ReasoningRequest | None] = {
    "default": None,
    "enabled": ReasoningRequest(mode="enabled"),
    "disabled": ReasoningRequest(mode="disabled"),
    "minimal": ReasoningRequest(mode="enabled", effort="minimal"),
    "low": ReasoningRequest(mode="enabled", effort="low"),
    "medium": ReasoningRequest(mode="enabled", effort="medium"),
    "high": ReasoningRequest(mode="enabled", effort="high"),
    "xhigh": ReasoningRequest(mode="enabled", effort="xhigh"),
    "max": ReasoningRequest(mode="enabled", effort="max"),
}


def _optional_json_string_list(raw: str, label: str) -> list[str] | None:
    value = _optional_json(raw, label)
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a JSON string array")
    return value


def _enabled_label(enabled: bool) -> str:
    return "On" if enabled else "Off"


def _toggle_member(values: set[str], value: str) -> None:
    if value in values:
        values.remove(value)
    else:
        values.add(value)


def _optional_json_mapping(raw: str, label: str) -> dict[str, str] | None:
    value = _optional_json(raw, label)
    if value is None:
        return None
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError(f"{label} must be a JSON object with string values")
    return dict(value)


def _optional_json(raw: str, label: str) -> object | None:
    normalized = raw.strip()
    if not normalized:
        return None
    try:
        return json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON: {exc.msg}") from exc


def _positive_int(raw: str, label: str) -> int:
    value = _integer(raw, label)
    if value < 1:
        raise ValueError(f"{label} must be positive")
    return value


def _optional_positive_int(raw: str, label: str) -> int | None:
    return _positive_int(raw, label) if raw.strip() else None


def _non_negative_int(raw: str, label: str) -> int:
    value = _integer(raw, label)
    if value < 0:
        raise ValueError(f"{label} must not be negative")
    return value


def _integer(raw: str, label: str) -> int:
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer") from exc


def _positive_float(raw: str, label: str) -> float:
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{label} must be positive")
    return value


def _ratio(raw: str, label: str) -> float:
    value = _positive_float(raw, label)
    if value > 1:
        raise ValueError(f"{label} must not exceed 1")
    return value


def _optional_bounded_float(
    raw: str,
    label: str,
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    if not raw.strip():
        return None
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}")
    return value


def _optional_text(value: object | None) -> str:
    return "" if value is None else str(value)
