from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ouro.config.credentials import CredentialRef
from ouro.config.endpoints import (
    ProviderEndpointConfig,
    endpoint_config_from_mapping,
    endpoint_config_to_public_dict,
    endpoint_config_to_raw,
    redact_sensitive_mapping,
)
from ouro.config.provider_headers import public_provider_headers
from ouro.config.provider_models import (
    ProviderConfig,
    normalize_provider_config,
)
from ouro.core.errors import ConfigError
from ouro.core.provider import ProviderRetryPolicy


def provider_config_to_public_dict(
    provider: ProviderConfig,
    *,
    is_default: bool = False,
) -> dict[str, Any]:
    """Render a credential-safe representation for CLI and API consumers."""

    return {
        "provider_id": provider.provider_id,
        "display_name": provider.display_name,
        "provider_type": provider.provider_type,
        "protocol": provider.protocol,
        "auth_driver": provider.auth_driver,
        "credential": provider.credential_ref.display(),
        "enabled": provider.enabled,
        "timeout": provider.timeout_seconds,
        "base_url": provider.base_url,
        "api_path": provider.api_path,
        "default": is_default,
        "default_endpoint": provider.default_endpoint,
        "endpoint_count": len(provider.endpoints),
        "provider_options": redact_sensitive_mapping(provider.extra),
        "headers": public_provider_headers(provider.headers),
        "endpoints": [
            {
                **endpoint_config_to_public_dict(endpoint),
                "default": endpoint.endpoint_id == provider.default_endpoint,
            }
            for endpoint in provider.endpoints
        ],
        "template": (
            {
                "template_id": provider.template.template_id,
                "origin": provider.template.origin,
                "revision": provider.template.revision,
            }
            if provider.template is not None
            else None
        ),
    }


def provider_config_from_mapping(
    provider_id: str,
    raw: Mapping[str, Any],
) -> ProviderConfig:
    """Decode the current nested provider/endpoint shape."""

    if "provider_id" in raw or "extra" in raw:
        raise ConfigError("provider_id belongs to the mapping key; options use provider_options")
    values = dict(raw)
    if "provider_options" in values:
        values["extra"] = values.pop("provider_options")
    try:
        provider = ProviderConfig.model_validate(
            {**values, "provider_id": provider_id, "endpoints": _endpoints_from_mapping(raw)},
        )
        return normalize_provider_config(provider)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid provider config {provider_id}: {exc}") from exc


def provider_config_to_raw(provider: ProviderConfig) -> dict[str, Any]:
    """Encode only the authoritative nested endpoint shape."""

    raw: dict[str, Any] = {
        "provider_type": provider.provider_type,
        "protocol": provider.protocol,
        "display_name": provider.display_name,
        "enabled": provider.enabled,
        "timeout_seconds": provider.timeout_seconds,
        "default_endpoint": provider.default_endpoint,
        "endpoints": {
            endpoint.endpoint_id: endpoint_config_to_raw(endpoint)
            for endpoint in provider.endpoints
        },
    }
    if provider.credential_ref.type != "none":
        raw["credential_ref"] = _credential_ref_to_raw(provider.credential_ref)
    if provider.auth_driver != "static":
        raw["auth_driver"] = provider.auth_driver
    if provider.base_url:
        raw["base_url"] = provider.base_url
    if provider.api_path:
        raw["api_path"] = provider.api_path
    # Persist only when the policy diverges from the default, so an explicit
    # opt-out (e.g. max_attempts=1) survives a round-trip instead of silently
    # reverting to the default on reload.
    if provider.retry_policy != ProviderRetryPolicy():
        raw["retry_policy"] = provider.retry_policy.model_dump(mode="json")
    if provider.headers:
        raw["headers"] = {name: item.model_dump() for name, item in provider.headers.items()}
    if provider.extra:
        raw["provider_options"] = provider.extra
    if provider.template is not None:
        raw["template"] = provider.template.model_dump(mode="json")
    return raw


def _endpoints_from_mapping(raw: Mapping[str, Any]) -> tuple[ProviderEndpointConfig, ...]:
    section = raw.get("endpoints")
    if section is None:
        return ()
    if not isinstance(section, Mapping):
        raise ValueError("endpoints config must be an object")
    endpoints: dict[str, ProviderEndpointConfig] = {}
    for endpoint_id, item in section.items():
        if not isinstance(endpoint_id, str) or not isinstance(item, Mapping):
            raise ValueError("endpoint entries must be named objects")
        endpoints[endpoint_id] = endpoint_config_from_mapping(endpoint_id, item)
    return tuple(endpoints[key] for key in sorted(endpoints))


def _credential_ref_to_raw(ref: CredentialRef) -> str | dict[str, str]:
    if ref.type == "env":
        return f"env:{ref.name}"
    if ref.type == "inline":
        return {"type": "inline", "value": ref.value or ""}
    if ref.type == "managed":
        assert ref.name is not None
        return {"type": "managed", "name": ref.name}
    return "none"
