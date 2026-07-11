from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator

from ouro.config.file import JsonConfigFile
from ouro.core.base import OuroModel
from ouro.core.compaction import CompactionPolicy
from ouro.core.errors import ConfigError


class CompactionConfig(OuroModel):
    strategy_id: str = "builtin.llm_summary"
    fallback_strategy_id: str | None = None
    policy: CompactionPolicy = Field(default_factory=CompactionPolicy)

    @model_validator(mode="after")
    def _validate_strategy_ids(self) -> CompactionConfig:
        if not self.strategy_id.strip():
            raise ValueError("compaction strategy_id must not be empty")
        if self.fallback_strategy_id is not None and not self.fallback_strategy_id.strip():
            raise ValueError("compaction fallback_strategy_id must not be empty")
        if self.fallback_strategy_id == self.strategy_id:
            raise ValueError("compaction fallback strategy must differ from primary strategy")
        return self


class CompactionConfigManager:
    def __init__(self, root: str | Path | None = None) -> None:
        self.file = JsonConfigFile(root)

    def snapshot(self) -> CompactionConfig:
        raw = self.file.read().get("compaction", {})
        if not isinstance(raw, dict):
            raise ConfigError("invalid compaction config: expected object")
        try:
            return CompactionConfig.model_validate(raw)
        except ValueError as exc:
            raise ConfigError(f"invalid compaction config: {exc}") from exc
