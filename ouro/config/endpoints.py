from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypedDict

from pydantic import ConfigDict, Field, field_validator, model_validator

from ouro.config.reasoning import EndpointReasoningConfig
from ouro.core.base import Metadata, MetadataField, OuroModel, new_id
from ouro.core.context_budget import ContextBudget

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
    reasoning_text_field: Literal["reasoning_content", "reasoning"] | None
    request_options: Mapping[str, Any]


class ProviderEndpointConfig(OuroModel):
    """Configuration and capabilities for one callable model endpoint."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

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
    reasoning_text_field: Literal["reasoning_content", "reasoning"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
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

    @field_validator("reasoning", "request_options", "metadata", mode="before")
    @classmethod
    def _optional_section(cls, value: object) -> object:
        return {} if value is None else value

    @model_validator(mode="after")
    def _validate_capabilities(self) -> ProviderEndpointConfig:
        ContextBudget(context_window=self.context_window, max_output_tokens=self.max_output_tokens)
        self.reasoning.validate_request_options(self.request_options)
        return self


def endpoint_config_from_mapping(
    endpoint_id: str,
    raw: Mapping[str, Any],
) -> ProviderEndpointConfig:
    if "endpoint_id" in raw:
        raise ValueError("endpoint_id belongs to the endpoint mapping key")
    return ProviderEndpointConfig.model_validate({**raw, "endpoint_id": endpoint_id})


def endpoint_config_to_raw(endpoint: ProviderEndpointConfig) -> dict[str, Any]:
    raw = endpoint.model_dump(
        exclude_none=True,
        exclude={"endpoint_id"}
        | {name for name in ("request_options", "metadata") if not getattr(endpoint, name)},
    )
    raw["max_output_tokens"] = endpoint.max_output_tokens
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


def _redact_public_value(value: object) -> object:
    if isinstance(value, Mapping):
        return redact_sensitive_mapping(value)
    if isinstance(value, list | tuple):
        return [_redact_public_value(item) for item in value]
    return value
