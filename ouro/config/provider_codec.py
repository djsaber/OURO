from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from pydantic import ValidationError

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
    SUPPORTED_PROVIDER_TYPES,
    ProviderConfig,
    ProviderProtocol,
    ProviderTemplateBinding,
    ProviderType,
    normalize_provider_config,
    validate_provider_protocol,
)
from ouro.core.errors import ConfigError
from ouro.core.provider import ProviderRetryPolicy

_PROVIDER_FIELDS = frozenset(
    {
        "provider_type",
        "protocol",
        "auth_driver",
        "display_name",
        "credential_ref",
        "default_endpoint",
        "endpoints",
        "base_url",
        "api_path",
        "enabled",
        "timeout_seconds",
        "retry_policy",
        "provider_options",
        "headers",
        "template",
    }
)


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

    unsupported = sorted(set(raw) - _PROVIDER_FIELDS)
    if unsupported:
        raise ConfigError(f"unsupported provider config fields: {', '.join(unsupported)}")

    try:
        provider = ProviderConfig(
            provider_id=provider_id,
            display_name=_required_str(raw.get("display_name"), "display_name"),
            provider_type=_provider_type(raw.get("provider_type")),
            protocol=_provider_protocol(raw.get("protocol")),
            auth_driver=raw.get("auth_driver", "static"),
            credential_ref=_credential_ref_from_mapping(raw),
            default_endpoint=_optional_str(raw.get("default_endpoint")),
            endpoints=_endpoints_from_mapping(raw),
            base_url=_optional_str(raw.get("base_url")),
            api_path=_optional_str(raw.get("api_path")),
            enabled=raw.get("enabled", True),
            timeout_seconds=raw.get("timeout_seconds", 60.0),
            retry_policy=ProviderRetryPolicy.model_validate(raw.get("retry_policy") or {}),
            extra=dict(raw.get("provider_options") or {}),
            headers=raw.get("headers", {}),
            template=_template_binding_from_mapping(raw.get("template")),
        )
        return normalize_provider_config(provider)
    except (TypeError, ValueError, ValidationError) as exc:
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
    if ref.type == "managed":
        assert ref.name is not None
        return {"type": "managed", "name": ref.name}
    return "none"


def _template_binding_from_mapping(raw: object) -> ProviderTemplateBinding | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("provider template binding must be an object")
    return ProviderTemplateBinding.model_validate(dict(raw))


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _required_str(value: object, field: str) -> str:
    normalized = _optional_str(value)
    if normalized is None:
        raise ValueError(f"provider config is missing {field}")
    return normalized


def _provider_type(value: object) -> ProviderType:
    if value in SUPPORTED_PROVIDER_TYPES:
        return cast(ProviderType, value)
    raise ValueError(f"unsupported provider type: {value}")


def _provider_protocol(value: object) -> ProviderProtocol:
    if isinstance(value, str):
        return validate_provider_protocol(value)
    raise ValueError(f"unsupported provider protocol: {value}")
