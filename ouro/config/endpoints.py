from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field, field_validator, model_validator

from ouro.core.base import Metadata, MetadataField, OuroModel
from ouro.core.provider import ThinkingLevel

_SENSITIVE_PUBLIC_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "auth_token",
        "password",
        "secret",
    }
)
_ENDPOINT_FIELDS = frozenset(
    {
        "endpoint_id",
        "model_id",
        "base_url",
        "api_path",
        "temperature",
        "context_window",
        "max_output_tokens",
        "supports_tools",
        "supports_vision",
        "supports_streaming",
        "supports_structured_output",
        "supports_reasoning",
        "supported_thinking_levels",
        "default_thinking_level",
        "request_options",
        "metadata",
    }
)


class ProviderEndpointConfig(OuroModel):
    """Configuration and capabilities for one callable model endpoint."""

    endpoint_id: str
    model_id: str
    base_url: str | None = None
    api_path: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    context_window: int = Field(default=128_000, ge=1)
    max_output_tokens: int = Field(default=8192, ge=1)
    supports_tools: bool = True
    supports_vision: bool = False
    supports_streaming: bool = True
    supports_structured_output: bool = False
    supports_reasoning: bool = False
    supported_thinking_levels: tuple[ThinkingLevel, ...] = Field(default_factory=tuple)
    default_thinking_level: ThinkingLevel | None = None
    request_options: Metadata = MetadataField
    metadata: Metadata = MetadataField

    @field_validator("endpoint_id", "model_id")
    @classmethod
    def _require_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().rstrip("/")
        return normalized or None

    @field_validator("api_path")
    @classmethod
    def _normalize_api_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        return normalized if normalized.startswith("/") else f"/{normalized}"

    @model_validator(mode="after")
    def _validate_capabilities(self) -> ProviderEndpointConfig:
        if self.max_output_tokens > self.context_window:
            raise ValueError("max_output_tokens must not exceed context_window")
        if len(set(self.supported_thinking_levels)) != len(self.supported_thinking_levels):
            raise ValueError("supported_thinking_levels must not contain duplicates")
        if not self.supports_reasoning and (
            self.supported_thinking_levels or self.default_thinking_level is not None
        ):
            raise ValueError("thinking levels require supports_reasoning=true")
        if (
            self.default_thinking_level is not None
            and self.supported_thinking_levels
            and self.default_thinking_level not in self.supported_thinking_levels
        ):
            raise ValueError("default_thinking_level must be listed in supported_thinking_levels")
        return self


def endpoint_config_from_mapping(
    endpoint_id: str,
    raw: Mapping[str, Any],
) -> ProviderEndpointConfig:
    unsupported = sorted(set(raw) - _ENDPOINT_FIELDS)
    if unsupported:
        raise ValueError(f"unsupported endpoint config fields: {', '.join(unsupported)}")
    configured_id = _optional_str(raw.get("endpoint_id")) or endpoint_id
    if configured_id != endpoint_id:
        raise ValueError(f"endpoint_id must match endpoints key ({configured_id} != {endpoint_id})")
    model_id = _optional_str(raw.get("model_id"))
    if model_id is None:
        raise ValueError(f"endpoint config is missing model_id: {endpoint_id}")
    return ProviderEndpointConfig(
        endpoint_id=configured_id,
        model_id=model_id,
        base_url=_optional_str(raw.get("base_url")),
        api_path=_optional_str(raw.get("api_path")),
        temperature=raw.get("temperature"),
        context_window=raw.get("context_window", 128_000),
        max_output_tokens=raw.get("max_output_tokens", 8192),
        supports_tools=raw.get("supports_tools", True),
        supports_vision=raw.get("supports_vision", False),
        supports_streaming=raw.get("supports_streaming", True),
        supports_structured_output=raw.get("supports_structured_output", False),
        supports_reasoning=raw.get("supports_reasoning", False),
        supported_thinking_levels=tuple(raw.get("supported_thinking_levels") or ()),
        default_thinking_level=raw.get("default_thinking_level"),
        request_options=dict(raw.get("request_options") or {}),
        metadata=dict(raw.get("metadata") or {}),
    )


def endpoint_config_to_raw(endpoint: ProviderEndpointConfig) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "endpoint_id": endpoint.endpoint_id,
        "model_id": endpoint.model_id,
        "context_window": endpoint.context_window,
        "max_output_tokens": endpoint.max_output_tokens,
        "supports_tools": endpoint.supports_tools,
        "supports_vision": endpoint.supports_vision,
        "supports_streaming": endpoint.supports_streaming,
        "supports_structured_output": endpoint.supports_structured_output,
        "supports_reasoning": endpoint.supports_reasoning,
    }
    if endpoint.base_url:
        raw["base_url"] = endpoint.base_url
    if endpoint.api_path:
        raw["api_path"] = endpoint.api_path
    if endpoint.temperature is not None:
        raw["temperature"] = endpoint.temperature
    if endpoint.supported_thinking_levels:
        raw["supported_thinking_levels"] = list(endpoint.supported_thinking_levels)
    if endpoint.default_thinking_level is not None:
        raw["default_thinking_level"] = endpoint.default_thinking_level
    if endpoint.request_options:
        raw["request_options"] = endpoint.request_options
    if endpoint.metadata:
        raw["metadata"] = endpoint.metadata
    return raw


def endpoint_config_to_public_dict(endpoint: ProviderEndpointConfig) -> dict[str, Any]:
    public = endpoint.model_dump(mode="json", exclude_none=True)
    for field in ("request_options", "metadata"):
        if field in public:
            public[field] = _redact_public_value(public[field])
    return public


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _redact_public_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "***"
                if str(key).casefold() in _SENSITIVE_PUBLIC_KEYS
                else _redact_public_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact_public_value(item) for item in value]
    return value
