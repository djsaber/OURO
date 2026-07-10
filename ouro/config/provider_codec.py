from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from ouro.config.credentials import CredentialRef
from ouro.config.endpoints import (
    ProviderEndpointConfig,
    endpoint_config_from_mapping,
    endpoint_config_to_public_dict,
    endpoint_config_to_raw,
)
from ouro.config.provider_models import (
    ProviderConfig,
    ProviderKind,
    normalize_provider_config,
)
from ouro.core.errors import ConfigError


def provider_config_to_public_dict(
    provider: ProviderConfig,
    *,
    is_default: bool = False,
) -> dict[str, Any]:
    """Render a credential-safe representation for CLI and API consumers."""

    default_endpoint = provider.endpoint()
    return {
        "provider_id": provider.provider_id,
        "kind": provider.provider_type,
        "credential": provider.credential_ref.display(),
        "enabled": provider.enabled,
        "timeout": provider.timeout_seconds,
        "default": is_default,
        "default_endpoint": provider.default_endpoint,
        "endpoint_count": len(provider.endpoints),
        "endpoints": [
            {
                **endpoint_config_to_public_dict(endpoint),
                "default": endpoint.endpoint_id == provider.default_endpoint,
            }
            for endpoint in provider.endpoints
        ],
        # Compatibility fields for existing CLI consumers.
        "model": default_endpoint.model_id if default_endpoint else None,
        "base_url": (
            (default_endpoint.base_url or provider.base_url)
            if default_endpoint
            else provider.base_url
        ),
    }


def provider_config_from_mapping(
    provider_id: str,
    raw: Mapping[str, Any],
) -> ProviderConfig:
    """Decode the current nested provider/endpoint shape."""

    configured_provider_id = _optional_str(raw.get("provider_id")) or provider_id
    if configured_provider_id != provider_id:
        raise ConfigError(
            "provider_id must match providers.items key "
            f"({configured_provider_id} != {provider_id})"
        )

    try:
        provider = ProviderConfig(
            provider_id=configured_provider_id,
            provider_type=_provider_kind(raw.get("provider_type") or "openai-compatible"),
            credential_ref=_credential_ref_from_mapping(raw),
            default_endpoint=_optional_str(raw.get("default_endpoint")),
            endpoints=_endpoints_from_mapping(raw),
            base_url=_optional_str(raw.get("base_url")),
            api_path=_optional_str(raw.get("api_path")),
            enabled=raw.get("enabled", True),
            timeout_seconds=raw.get("timeout_seconds", 60.0),
            retry_policy=dict(raw.get("retry_policy") or {}),
            extra=dict(raw.get("provider_options") or {}),
        )
        return normalize_provider_config(provider)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ConfigError(f"invalid provider config {provider_id}: {exc}") from exc


def provider_config_to_raw(provider: ProviderConfig) -> dict[str, Any]:
    """Encode only the authoritative nested endpoint shape."""

    raw: dict[str, Any] = {
        "provider_type": provider.provider_type,
        "provider_id": provider.provider_id,
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
    if provider.base_url:
        raw["base_url"] = provider.base_url
    if provider.api_path:
        raw["api_path"] = provider.api_path
    if provider.retry_policy:
        raw["retry_policy"] = provider.retry_policy
    if provider.extra:
        raw["provider_options"] = provider.extra
    return raw


def _endpoints_from_mapping(raw: Mapping[str, Any]) -> tuple[ProviderEndpointConfig, ...]:
    section = raw.get("endpoints")
    if section is None:
        return ()
    endpoints: dict[str, ProviderEndpointConfig] = {}
    if isinstance(section, Mapping):
        for endpoint_id, item in section.items():
            if not isinstance(endpoint_id, str) or not isinstance(item, Mapping):
                raise ValueError("endpoint entries must be named objects")
            endpoint = endpoint_config_from_mapping(endpoint_id, item)
            _add_unique_endpoint(endpoints, endpoint)
    elif isinstance(section, list):
        for item in section:
            if not isinstance(item, Mapping):
                raise ValueError("endpoint config list entries must be objects")
            endpoint_id = _optional_str(item.get("endpoint_id"))
            if endpoint_id is None:
                raise ValueError("endpoint config entry is missing endpoint_id")
            endpoint = endpoint_config_from_mapping(endpoint_id, item)
            _add_unique_endpoint(endpoints, endpoint)
    else:
        raise ValueError("endpoints config must be an object or list")
    return tuple(endpoints[key] for key in sorted(endpoints))


def _add_unique_endpoint(
    endpoints: dict[str, ProviderEndpointConfig],
    endpoint: ProviderEndpointConfig,
) -> None:
    if endpoint.endpoint_id in endpoints:
        raise ValueError(f"duplicate endpoint id: {endpoint.endpoint_id}")
    endpoints[endpoint.endpoint_id] = endpoint


def _credential_ref_from_mapping(raw: Mapping[str, Any]) -> CredentialRef:
    credential_ref = raw.get("credential_ref")
    if isinstance(credential_ref, str) or credential_ref is None:
        if credential_ref is not None:
            return CredentialRef.parse(credential_ref)
    elif isinstance(credential_ref, Mapping):
        return CredentialRef.model_validate(dict(credential_ref))
    if credential_ref is not None:
        return CredentialRef.model_validate(credential_ref)
    return CredentialRef()


def _credential_ref_to_raw(ref: CredentialRef) -> str | dict[str, str]:
    if ref.type == "env":
        return f"env:{ref.name}"
    if ref.type == "inline":
        return {"type": "inline", "value": ref.value or ""}
    return "none"


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _provider_kind(value: object) -> ProviderKind:
    if value in {"openai-compatible", "openai_compatible"}:
        return "openai-compatible"
    if value == "faux":
        return "faux"
    raise ValueError(f"unsupported provider kind: {value}")
