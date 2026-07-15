from __future__ import annotations

from copy import deepcopy
from typing import Any

from ouro.config.reasoning import ReasoningProfileConfig
from ouro.core.provider import ModelMetadata


class ReasoningPayloadEncoder:
    """Apply a validated endpoint profile to one native provider payload."""

    def __init__(self, profile: ReasoningProfileConfig | None) -> None:
        self.profile = profile

    def apply(self, payload: dict[str, Any], model: ModelMetadata) -> None:
        if self.profile is None:
            return
        resolution = model.reasoning_resolution
        fragment = self.profile.variants.get(resolution.variant)
        if fragment is None:
            if resolution.variant == "default":
                return
            raise ValueError(
                f"reasoning profile {self.profile.name!r} has no variant for {resolution.variant!r}"
            )
        payload.update(deepcopy(fragment))
