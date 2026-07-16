from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError

from ouro.config.file import JsonConfigFile, atomic_config_update
from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError
from ouro.core.tool_batches import ToolExecutionMode


class ToolExecutionConfig(OuroModel):
    mode: ToolExecutionMode = "sequential"
    max_concurrency: int = Field(default=4, ge=1, le=64)
    workspace_only: bool = True
    max_output_chars: int = Field(default=100_000, ge=1024, le=10_000_000)


class ToolExecutionConfigManager:
    def __init__(self, root: str | Path | None = None) -> None:
        self._file = JsonConfigFile(root)

    @property
    def file(self) -> JsonConfigFile:
        return self._file

    def snapshot(self) -> ToolExecutionConfig:
        raw = self._file.read().get("tool_execution", {})
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ConfigError("tool_execution config must be an object")
        try:
            return ToolExecutionConfig.model_validate(dict(raw))
        except ValidationError as exc:
            raise ConfigError(f"invalid tool_execution config: {exc}") from exc

    @atomic_config_update
    def update(
        self,
        *,
        mode: ToolExecutionMode | None = None,
        max_concurrency: int | None = None,
        workspace_only: bool | None = None,
        max_output_chars: int | None = None,
    ) -> ToolExecutionConfig:
        current = self.snapshot()
        updated = current.model_copy(
            update={
                "mode": current.mode if mode is None else mode,
                "max_concurrency": current.max_concurrency
                if max_concurrency is None
                else max_concurrency,
                "workspace_only": current.workspace_only
                if workspace_only is None
                else workspace_only,
                "max_output_chars": current.max_output_chars
                if max_output_chars is None
                else max_output_chars,
            }
        )
        try:
            updated = ToolExecutionConfig.model_validate(updated.model_dump())
        except ValidationError as exc:
            raise ConfigError(f"invalid tool_execution config: {exc}") from exc
        raw: dict[str, Any] = deepcopy(self._file.read())
        raw["tool_execution"] = updated.model_dump(mode="json")
        self._file.write(raw)
        return updated


def tool_execution_config_to_public_dict(config: ToolExecutionConfig) -> dict[str, Any]:
    return config.model_dump(mode="json")
