"""Validated provider preset contract, independent of the built-in catalog."""

from __future__ import annotations

from pydantic import ConfigDict, Field, field_validator, model_validator

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_headers import ProviderHeaderValue, parse_provider_headers
from ouro.config.provider_models import (
    ProviderProtocol,
    ProviderType,
    resolve_provider_connection,
    validate_media_protocol_compatibility,
)
from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError


class ProviderTemplate(OuroModel):
    """A reviewed provider configuration preset with callable model endpoints."""

    model_config = ConfigDict(frozen=True)

    template_id: str
    display_name: str
    description: str
    provider_type: ProviderType
    protocol: ProviderProtocol
    auth_driver: str = "static"
    credential_env: str | None = None
    base_url: str
    api_path: str
    headers: dict[str, ProviderHeaderValue] = Field(default_factory=dict, repr=False)
    default_endpoint: str
    endpoints: tuple[ProviderEndpointConfig, ...]
    documentation_url: str
    verified_on: str
    revision: int = Field(default=1, ge=1)

    _validate_headers = field_validator("headers", mode="before")(parse_provider_headers)

    @field_validator("template_id", "display_name", "default_endpoint")
    @classmethod
    def _require_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @field_validator("credential_env")
    @classmethod
    def _validate_credential_env(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return cls._require_identifier(value)

    @model_validator(mode="after")
    def _validate_endpoint_registry(self) -> ProviderTemplate:
        resolve_provider_connection(self.provider_type, self.protocol, self.auth_driver)
        if self.auth_driver != "static" and self.credential_env is not None:
            raise ValueError("managed templates cannot declare credential_env")
        if self.protocol == "faux" and self.headers:
            raise ValueError("faux protocol does not accept HTTP headers")
        endpoint_ids = [endpoint.endpoint_id for endpoint in self.endpoints]
        if not endpoint_ids:
            raise ValueError("provider template must contain at least one endpoint")
        if len(set(endpoint_ids)) != len(endpoint_ids):
            raise ValueError("provider template endpoint ids must be unique")
        if self.default_endpoint not in endpoint_ids:
            raise ValueError(f"default endpoint not found: {self.default_endpoint}")
        for endpoint in self.endpoints:
            validate_media_protocol_compatibility(
                self.protocol,
                supports_image_input=endpoint.supports_image_input,
                supports_audio_input=endpoint.supports_audio_input,
                supports_video_input=endpoint.supports_video_input,
            )
        return self

    def endpoint(self, endpoint_id: str | None = None) -> ProviderEndpointConfig:
        target = endpoint_id or self.default_endpoint
        endpoint = next(
            (item for item in self.endpoints if item.endpoint_id == target),
            None,
        )
        if endpoint is None:  # Protected by validation for the default lookup.
            raise ConfigError(f"provider template endpoint not found: {self.template_id}/{target}")
        return endpoint
