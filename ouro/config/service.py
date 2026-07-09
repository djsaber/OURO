from __future__ import annotations

from copy import deepcopy
from typing import Any


class ConfigService:
    """Merge simple dict-based configuration layers using OURO precedence."""

    def __init__(self, *layers: dict[str, Any] | None) -> None:
        self._layers = [deepcopy(layer) for layer in layers if layer]

    def merged(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for layer in reversed(self._layers):
            result = _deep_merge(result, layer)
        return result

    def get(self, key: str, default: Any = None) -> Any:
        current: Any = self.merged()
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged
