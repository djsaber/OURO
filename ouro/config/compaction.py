from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator, model_validator

from ouro.config.file import JsonConfigFile
from ouro.core.base import OuroModel
from ouro.core.compaction import CompactionPolicy
from ouro.core.errors import ConfigError
from ouro.core.provider import ThinkingLevel


class CompactionModelConfig(OuroModel):
    """Endpoint selection and request-scoped model overrides for compaction only."""

    provider_id: str | None = None
    endpoint_id: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_output_tokens: int | None = Field(default=None, ge=1)
    thinking_enabled: bool | None = None
    thinking_level: ThinkingLevel | None = None

    @field_validator("provider_id", "endpoint_id")
    @classmethod
    def _normalize_optional_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def _validate_model_options(self) -> CompactionModelConfig:
        if (self.provider_id is None) != (self.endpoint_id is None):
            raise ValueError(
                "compaction model provider_id and endpoint_id must be configured together"
            )
        if self.thinking_enabled is False and self.thinking_level not in {None, "off"}:
            raise ValueError("compaction thinking_level must be off when thinking_enabled is false")
        if self.thinking_enabled is True and self.thinking_level == "off":
            raise ValueError(
                "compaction thinking_level cannot be off when thinking_enabled is true"
            )
        return self

    @property
    def has_dedicated_endpoint(self) -> bool:
        return self.provider_id is not None and self.endpoint_id is not None


class CompactionConfig(OuroModel):
    strategy_id: str = "builtin.llm_summary"
    fallback_strategy_id: str | None = None
    policy: CompactionPolicy = Field(default_factory=CompactionPolicy)
    model: CompactionModelConfig = Field(default_factory=CompactionModelConfig)

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

    def save(self, config: CompactionConfig) -> CompactionConfig:
        try:
            validated = CompactionConfig.model_validate(config.model_dump())
        except ValueError as exc:
            raise ConfigError(f"invalid compaction config: {exc}") from exc
        raw = self.file.read()
        raw["compaction"] = validated.model_dump(mode="json", exclude_none=True)
        self.file.write(raw)
        return validated

    def set_model(self, model: CompactionModelConfig) -> CompactionConfig:
        return self.save(self.snapshot().model_copy(update={"model": model}))

    def update_model(
        self,
        *,
        provider_id: str | None = None,
        endpoint_id: str | None = None,
        use_runtime_endpoint: bool = False,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        thinking_enabled: bool | None = None,
        thinking_level: ThinkingLevel | None = None,
        clear: bool = False,
    ) -> CompactionConfig:
        """Merge one validated CLI/application update into persisted model settings."""

        if use_runtime_endpoint and (provider_id is not None or endpoint_id is not None):
            raise ConfigError(
                "use_runtime_endpoint cannot be combined with provider_id or endpoint_id"
            )
        if clear and any(
            (
                provider_id is not None,
                endpoint_id is not None,
                use_runtime_endpoint,
                temperature is not None,
                max_output_tokens is not None,
                thinking_enabled is not None,
                thinking_level is not None,
            )
        ):
            raise ConfigError("clear cannot be combined with compaction model options")

        current = self.snapshot()
        model = CompactionModelConfig() if clear else current.model
        updates: dict[str, object] = {}
        if use_runtime_endpoint:
            updates.update({"provider_id": None, "endpoint_id": None})
        elif provider_id is not None or endpoint_id is not None:
            updates.update(
                {
                    "provider_id": provider_id or model.provider_id,
                    "endpoint_id": endpoint_id or model.endpoint_id,
                }
            )
        if temperature is not None:
            updates["temperature"] = temperature
        if max_output_tokens is not None:
            updates["max_output_tokens"] = max_output_tokens
        if thinking_enabled is not None:
            updates["thinking_enabled"] = thinking_enabled
        if thinking_level is not None:
            updates["thinking_level"] = thinking_level
        try:
            updated = CompactionModelConfig.model_validate(
                model.model_copy(update=updates).model_dump()
            )
        except ValueError as exc:
            raise ConfigError(f"invalid compaction config: {exc}") from exc
        return self.set_model(updated)


def compaction_config_to_public_dict(config: CompactionConfig) -> dict[str, object]:
    return config.model_dump(mode="json", exclude_none=True)
