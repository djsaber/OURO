from __future__ import annotations

from copy import deepcopy
from typing import Any

from ouro.config.reasoning import ReasoningProfileConfig
from ouro.core.provider import ModelMetadata


def _merge_fragment(target: dict[str, Any], fragment: dict[str, Any], path: str = "") -> None:
    for key, value in fragment.items():
        field = f"{path}.{key}" if path else key
        if key not in target:
            target[key] = deepcopy(value)
        elif isinstance(target[key], dict) and isinstance(value, dict):
            _merge_fragment(target[key], value, field)
        elif target[key] != value:
            raise ValueError(f"reasoning mappings conflict at {field}")


class ReasoningPayloadEncoder:
    """Apply a validated endpoint profile to one native provider payload."""

    def __init__(self, profile: ReasoningProfileConfig | None) -> None:
        self.profile = profile

    def apply(self, payload: dict[str, Any], model: ModelMetadata) -> None:
        if self.profile is None:
            return
        resolution = model.reasoning_resolution
        collisions = sorted(self.profile.owned_fields.intersection(payload))
        if collisions:
            raise ValueError(
                "request options conflict with reasoning-owned fields: " + ", ".join(collisions)
            )
        encoded: dict[str, Any] = {}
        fragment = self.profile.effort.get(resolution.effort_variant)
        if fragment is None:
            if resolution.effort_variant != "default":
                raise ValueError(
                    f"reasoning profile {self.profile.name!r} "
                    f"has no effort variant for {resolution.effort_variant!r}"
                )
        else:
            _merge_fragment(encoded, fragment)
        summary = resolution.effective.summary
        if (
            summary is not None
            and summary != "provider_default"
            and resolution.effective.mode != "disabled"
        ):
            fragment = self.profile.summary.get(summary)
            if fragment is None:
                raise ValueError(
                    f"reasoning profile {self.profile.name!r} "
                    f"has no summary variant for {summary!r}"
                )
            _merge_fragment(encoded, fragment)
        payload.update(encoded)
