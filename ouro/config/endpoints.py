from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict

from pydantic import ConfigDict, Field, field_validator, model_validator

from ouro.config.reasoning import EndpointReasoningConfig
from ouro.core.base import Metadata, MetadataField, OuroModel, new_id
from ouro.core.context_budget import ContextBudget
from ouro.core.errors import UnsupportedConfigVersionError

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
        "display_name",
        "model_id",
        "base_url",
        "api_path",
        "temperature",
        "context_window",
        "max_output_tokens",
        "supports_tools",
        "supports_image_input",
        "supports_audio_input",
        "supports_video_input",
        "supports_streaming",
        "supports_structured_output",
        "reasoning",
        "request_options",
        "metadata",
    }
)


class EndpointChanges(TypedDict, total=False):
    """Fields explicitly submitted by an endpoint editor."""

    display_name: str | None
    model_id: str
    base_url: str | None
    api_path: str | None
    temperature: float | None
    context_window: int
    max_output_tokens: int | None
    supports_tools: bool
    supports_image_input: bool
    supports_audio_input: bool
    supports_video_input: bool
    supports_streaming: bool
    supports_structured_output: bool
    reasoning: EndpointReasoningConfig
    request_options: Mapping[str, Any]


class ProviderEndpointConfig(OuroModel):
    """Configuration and capabilities for one callable model endpoint."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: str = Field(default_factory=lambda: new_id("endpoint"))
    display_name: str | None = None
    model_id: str
    base_url: str | None = None
    api_path: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    context_window: int = Field(default=1_000_000, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1, strict=True)
    supports_tools: bool = True
    supports_image_input: bool = False
    supports_audio_input: bool = False
    supports_video_input: bool = False
    supports_streaming: bool = True
    supports_structured_output: bool = True
    reasoning: EndpointReasoningConfig = Field(default_factory=EndpointReasoningConfig)
    request_options: Metadata = MetadataField
    metadata: Metadata = MetadataField

    @field_validator("endpoint_id", "model_id")
    @classmethod
    def _require_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("display_name")
    @classmethod
    def _normalize_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

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
        ContextBudget(context_window=self.context_window, max_output_tokens=self.max_output_tokens)
        self.reasoning.validate_request_options(self.request_options)
        return self


def endpoint_config_from_mapping(
    endpoint_id: str,
    raw: Mapping[str, Any],
) -> ProviderEndpointConfig:
    unsupported = sorted(set(raw) - _ENDPOINT_FIELDS)
    if unsupported == ["supports_vision"]:
        raise UnsupportedConfigVersionError(
            "unsupported endpoint capability format: supports_vision; run ouro-upgrade"
        )
    if unsupported:
        raise ValueError(f"unsupported endpoint config fields: {', '.join(unsupported)}")
    model_id = _optional_str(raw.get("model_id"))
    if model_id is None:
        raise ValueError(f"endpoint config is missing model_id: {endpoint_id}")
    return ProviderEndpointConfig(
        endpoint_id=endpoint_id,
        display_name=_optional_str(raw.get("display_name")),
        model_id=model_id,
        base_url=_optional_str(raw.get("base_url")),
        api_path=_optional_str(raw.get("api_path")),
        temperature=raw.get("temperature"),
        context_window=raw.get("context_window", 1_000_000),
        max_output_tokens=raw.get("max_output_tokens"),
        supports_tools=raw.get("supports_tools", True),
        supports_image_input=raw.get("supports_image_input", False),
        supports_audio_input=raw.get("supports_audio_input", False),
        supports_video_input=raw.get("supports_video_input", False),
        supports_streaming=raw.get("supports_streaming", True),
        supports_structured_output=raw.get("supports_structured_output", True),
        reasoning=EndpointReasoningConfig.model_validate(raw.get("reasoning") or {}),
        request_options=dict(raw.get("request_options") or {}),
        metadata=dict(raw.get("metadata") or {}),
    )


def endpoint_config_to_raw(endpoint: ProviderEndpointConfig) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "model_id": endpoint.model_id,
        "context_window": endpoint.context_window,
        "max_output_tokens": endpoint.max_output_tokens,
        "supports_tools": endpoint.supports_tools,
        "supports_image_input": endpoint.supports_image_input,
        "supports_audio_input": endpoint.supports_audio_input,
        "supports_video_input": endpoint.supports_video_input,
        "supports_streaming": endpoint.supports_streaming,
        "supports_structured_output": endpoint.supports_structured_output,
        "reasoning": endpoint.reasoning.model_dump(mode="json", exclude_none=True),
    }
    if endpoint.display_name:
        raw["display_name"] = endpoint.display_name
    if endpoint.base_url:
        raw["base_url"] = endpoint.base_url
    if endpoint.api_path:
        raw["api_path"] = endpoint.api_path
    if endpoint.temperature is not None:
        raw["temperature"] = endpoint.temperature
    if endpoint.request_options:
        raw["request_options"] = endpoint.request_options
    if endpoint.metadata:
        raw["metadata"] = endpoint.metadata
    return raw


def endpoint_config_to_public_dict(endpoint: ProviderEndpointConfig) -> dict[str, Any]:
    public = endpoint.model_dump(mode="json", exclude_none=True)
    for field in ("reasoning", "request_options", "metadata"):
        if field in public:
            public[field] = _redact_public_value(public[field])
    return public


def redact_sensitive_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a recursively redacted copy suitable for public configuration views."""

    return {
        str(key): (
            "***" if str(key).casefold() in _SENSITIVE_PUBLIC_KEYS else _redact_public_value(item)
        )
        for key, item in value.items()
    }


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _redact_public_value(value: object) -> object:
    if isinstance(value, Mapping):
        return redact_sensitive_mapping(value)
    if isinstance(value, list | tuple):
        return [_redact_public_value(item) for item in value]
    return value
