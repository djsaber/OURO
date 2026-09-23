from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import Field, field_validator, model_validator

from ouro.config.file import JsonConfigFile, atomic_config_update
from ouro.config.providers import ProviderConfigManager
from ouro.core.base import OuroModel
from ouro.core.compaction import CompactionPolicy
from ouro.core.errors import ConfigError
from ouro.core.reasoning import ReasoningRequest


class CompactionModelConfig(OuroModel):
    """Dedicated route and request overrides owned by the compaction policy.

    A Session runtime route owns its complete model request configuration.  A
    compaction-specific override therefore only has meaning with a dedicated
    endpoint.
    """

    provider_id: str | None = None
    endpoint_id: str | None = None
    use_default_model: bool = False
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_output_tokens: int | None = Field(default=None, ge=1)
    reasoning: ReasoningRequest | None = None

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
        if self.use_default_model and self.has_dedicated_endpoint:
            raise ValueError("default model cannot be combined with a dedicated endpoint")
        has_request_overrides = any(
            value is not None
            for value in (self.temperature, self.max_output_tokens, self.reasoning)
        )
        if has_request_overrides and not self.has_configured_route:
            raise ValueError(
                "compaction model request overrides require a default or dedicated model endpoint"
            )
        return self

    @property
    def has_dedicated_endpoint(self) -> bool:
        return self.provider_id is not None and self.endpoint_id is not None

    @property
    def has_configured_route(self) -> bool:
        return self.use_default_model or self.has_dedicated_endpoint


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

    @atomic_config_update
    def save(self, config: CompactionConfig) -> CompactionConfig:
        try:
            validated = CompactionConfig.model_validate(config.model_dump())
        except ValueError as exc:
            raise ConfigError(f"invalid compaction config: {exc}") from exc
        if validated.model.use_default_model:
            ProviderConfigManager(self.file.root).require_default_route()
        raw = self.file.read()
        raw["compaction"] = validated.model_dump(mode="json", exclude_none=True)
        self.file.write(raw)
        return validated

    @atomic_config_update
    def set_model(self, model: CompactionModelConfig) -> CompactionConfig:
        return self.save(self.snapshot().model_copy(update={"model": model}))

    @atomic_config_update
    def update_model(
        self,
        *,
        provider_id: str | None = None,
        endpoint_id: str | None = None,
        use_runtime_endpoint: bool = False,
        use_default_model: bool = False,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        reasoning: ReasoningRequest | None = None,
        clear: bool = False,
        validate: Callable[[CompactionModelConfig], None] | None = None,
    ) -> CompactionConfig:
        """Merge one validated CLI/application update into persisted model settings."""

        model = self._prepare_model_update(
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            use_runtime_endpoint=use_runtime_endpoint,
            use_default_model=use_default_model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            reasoning=reasoning,
            clear=clear,
        )
        if validate is not None:
            validate(model)
        return self.set_model(model)

    def _prepare_model_update(
        self,
        *,
        provider_id: str | None = None,
        endpoint_id: str | None = None,
        use_runtime_endpoint: bool = False,
        use_default_model: bool = False,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        reasoning: ReasoningRequest | None = None,
        clear: bool = False,
    ) -> CompactionModelConfig:
        """Build a validated model update without mutating durable configuration."""

        if (
            use_runtime_endpoint
            and any(
                value is not None
                for value in (
                    provider_id,
                    endpoint_id,
                    temperature,
                    max_output_tokens,
                    reasoning,
                )
            )
            or (use_runtime_endpoint and use_default_model)
        ):
            raise ConfigError(
                "use_runtime_endpoint cannot be combined with a model route or request override"
            )
        if use_default_model and (provider_id is not None or endpoint_id is not None):
            raise ConfigError("default model cannot be combined with a dedicated endpoint")
        if clear and any(
            (
                provider_id is not None,
                endpoint_id is not None,
                use_runtime_endpoint,
                use_default_model,
                temperature is not None,
                max_output_tokens is not None,
                reasoning is not None,
            )
        ):
            raise ConfigError("clear cannot be combined with compaction model options")

        current = self.snapshot()
        model = CompactionModelConfig() if clear else current.model
        updates: dict[str, object] = {}
        if use_runtime_endpoint:
            updates.update(
                {
                    "provider_id": None,
                    "endpoint_id": None,
                    "use_default_model": False,
                    "temperature": None,
                    "max_output_tokens": None,
                    "reasoning": None,
                }
            )
        elif use_default_model:
            updates.update({"provider_id": None, "endpoint_id": None, "use_default_model": True})
        elif provider_id is not None or endpoint_id is not None:
            updates.update(
                {
                    "provider_id": provider_id or model.provider_id,
                    "endpoint_id": endpoint_id or model.endpoint_id,
                    "use_default_model": False,
                }
            )
        if temperature is not None:
            updates["temperature"] = temperature
        if max_output_tokens is not None:
            updates["max_output_tokens"] = max_output_tokens
        if reasoning is not None:
            updates["reasoning"] = reasoning
        try:
            return CompactionModelConfig.model_validate(
                model.model_copy(update=updates).model_dump()
            )
        except ValueError as exc:
            raise ConfigError(f"invalid compaction config: {exc}") from exc


def compaction_config_to_public_dict(config: CompactionConfig) -> dict[str, object]:
    return config.model_dump(mode="json", exclude_none=True)
