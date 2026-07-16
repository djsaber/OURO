from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from ouro.config.file import JsonConfigFile, atomic_config_update
from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError

QueuedInputConsumptionMode = Literal["one_by_one", "all_at_once"]


class QueuedInputConfig(OuroModel):
    """Configure how eligible queued user inputs are presented to the model."""

    consumption_mode: QueuedInputConsumptionMode = "one_by_one"


class QueuedInputConfigManager:
    def __init__(self, root: str | Path | None = None) -> None:
        self._file = JsonConfigFile(root)

    @property
    def file(self) -> JsonConfigFile:
        return self._file

    def snapshot(self) -> QueuedInputConfig:
        raw = self._file.read().get("queued_inputs", {})
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ConfigError("queued_inputs config must be an object")
        try:
            return QueuedInputConfig.model_validate(dict(raw))
        except ValidationError as exc:
            raise ConfigError(f"invalid queued_inputs config: {exc}") from exc

    @atomic_config_update
    def update(
        self,
        *,
        consumption_mode: QueuedInputConsumptionMode | None = None,
    ) -> QueuedInputConfig:
        current = self.snapshot()
        updated = current.model_copy(
            update={
                "consumption_mode": current.consumption_mode
                if consumption_mode is None
                else consumption_mode,
            }
        )
        try:
            updated = QueuedInputConfig.model_validate(updated.model_dump())
        except ValidationError as exc:
            raise ConfigError(f"invalid queued_inputs config: {exc}") from exc
        raw: dict[str, Any] = deepcopy(self._file.read())
        raw["queued_inputs"] = updated.model_dump(mode="json")
        self._file.write(raw)
        return updated


def queued_input_config_to_public_dict(config: QueuedInputConfig) -> dict[str, Any]:
    return config.model_dump(mode="json")
