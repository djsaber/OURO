from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from ouro.config.credentials import CredentialRef
from ouro.config.endpoints import ProviderEndpointConfig
from ouro.core.base import Metadata, MetadataField, OuroModel
from ouro.core.provider import ThinkingLevel

ProviderKind = Literal["faux", "openai-compatible"]
SUPPORTED_PROVIDER_KINDS: tuple[ProviderKind, ...] = ("faux", "openai-compatible")
BUILTIN_FAUX_PROVIDER_ID = "faux"


class ProviderConfig(OuroModel):
    """Supplier-level connection configuration and its endpoint registry."""

    provider_id: str
    provider_type: ProviderKind = "openai-compatible"
    credential_ref: CredentialRef = Field(default_factory=CredentialRef)
    default_endpoint: str | None = None
    endpoints: tuple[ProviderEndpointConfig, ...] = Field(default_factory=tuple)
    enabled: bool = True
    timeout_seconds: float = Field(default=60.0, gt=0)
    retry_policy: dict[str, int | float] = Field(default_factory=dict)
    extra: Metadata = MetadataField

    @field_validator("provider_id")
    @classmethod
    def _require_provider_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("provider_id must not be empty")
        return normalized

    @field_validator("provider_type", mode="before")
    @classmethod
    def _normalize_provider_type(cls, value: object) -> object:
        return "openai-compatible" if value == "openai_compatible" else value

    @field_validator("credential_ref", mode="before")
    @classmethod
    def _parse_credential_ref(cls, value: object) -> object:
        if isinstance(value, str) or value is None:
            return CredentialRef.parse(value)
        return value

    @model_validator(mode="after")
    def _validate_endpoint_registry(self) -> ProviderConfig:
        endpoint_ids = [endpoint.endpoint_id for endpoint in self.endpoints]
        if len(set(endpoint_ids)) != len(endpoint_ids):
            raise ValueError("endpoint ids must be unique")
        if self.default_endpoint is not None and self.default_endpoint not in endpoint_ids:
            raise ValueError(f"default endpoint not found: {self.default_endpoint}")
        if self.provider_type == "openai-compatible":
            missing_base_url = next(
                (endpoint.endpoint_id for endpoint in self.endpoints if not endpoint.base_url),
                None,
            )
            if missing_base_url is not None:
                raise ValueError(
                    f"base_url is required for endpoint: {self.provider_id}/{missing_base_url}"
                )
        return self

    def endpoint(self, endpoint_id: str | None = None) -> ProviderEndpointConfig | None:
        target = endpoint_id or self.default_endpoint
        if target is None:
            return None
        return next(
            (endpoint for endpoint in self.endpoints if endpoint.endpoint_id == target),
            None,
        )


@dataclass(frozen=True)
class ProviderRuntimeOptions:
    provider: ProviderKind | None = None
    provider_id: str | None = None
    endpoint_id: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_path: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    credential_ref: CredentialRef | None = None
    timeout: float | None = None
    temperature: float | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    supports_streaming: bool | None = None
    supports_structured_output: bool | None = None
    supports_reasoning: bool | None = None
    supported_thinking_levels: tuple[ThinkingLevel, ...] | None = None
    thinking_level: ThinkingLevel | None = None
    extra: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProviderConfigSnapshot:
    default_provider_id: str
    providers: tuple[ProviderConfig, ...]
    config_path: Path


def normalize_provider_config(provider: ProviderConfig) -> ProviderConfig:
    """Install provider-type defaults without performing persistence work."""

    if provider.provider_type == "faux" and not provider.endpoints:
        endpoint = ProviderEndpointConfig(endpoint_id="faux", model_id="faux")
        return ProviderConfig.model_validate(
            provider.model_copy(
                update={
                    "default_endpoint": endpoint.endpoint_id,
                    "endpoints": (endpoint,),
                }
            ).model_dump()
        )
    return provider


def builtin_faux_provider() -> ProviderConfig:
    endpoint = ProviderEndpointConfig(endpoint_id="faux", model_id="faux")
    return ProviderConfig(
        provider_id=BUILTIN_FAUX_PROVIDER_ID,
        provider_type="faux",
        default_endpoint=endpoint.endpoint_id,
        endpoints=(endpoint,),
        enabled=True,
        timeout_seconds=60.0,
    )
