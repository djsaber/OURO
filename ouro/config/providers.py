from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic.functional_validators import field_validator

from ouro.config.credentials import CredentialRef
from ouro.config.file import JsonConfigFile
from ouro.core.base import Metadata, MetadataField, OuroModel
from ouro.core.errors import ConfigError

ProviderKind = Literal["faux", "openai-compatible"]
SUPPORTED_PROVIDER_KINDS: tuple[ProviderKind, ...] = ("faux", "openai-compatible")
BUILTIN_FAUX_PROVIDER_ID = "faux"


class ProviderConfig(OuroModel):
    provider_id: str
    provider_type: str = "openai-compatible"
    base_url: str | None = None
    credential_ref: CredentialRef = Field(default_factory=CredentialRef)
    default_model: str | None = None
    enabled: bool = True
    timeout_seconds: float = 60.0
    retry_policy: dict[str, int | float] = Field(default_factory=dict)
    extra: Metadata = MetadataField

    @field_validator("credential_ref", mode="before")
    @classmethod
    def _parse_credential_ref(cls, value: object) -> object:
        if isinstance(value, str) or value is None:
            return CredentialRef.parse(value)
        return value


@dataclass(frozen=True)
class ProviderRuntimeOptions:
    provider: ProviderKind | None = None
    provider_id: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    credential_ref: CredentialRef | None = None
    timeout: float | None = None


@dataclass(frozen=True)
class ProviderConfigSnapshot:
    default_provider_id: str
    providers: tuple[ProviderConfig, ...]
    config_path: Path


class ProviderConfigManager:
    def __init__(self, root: str | Path | None = None) -> None:
        self._file = JsonConfigFile(root)
        self.root = self._file.root
        self.config_path = self._file.path

    def snapshot(self, *, include_builtin: bool = True) -> ProviderConfigSnapshot:
        raw = self.load_raw()
        configured = self._configured_providers(raw)
        default_provider_id = self._default_provider_id(raw, configured)
        providers = dict(configured)
        if include_builtin:
            providers.setdefault(BUILTIN_FAUX_PROVIDER_ID, _builtin_faux_provider())
            if default_provider_id not in providers:
                default_provider_id = BUILTIN_FAUX_PROVIDER_ID
        return ProviderConfigSnapshot(
            default_provider_id=default_provider_id,
            providers=tuple(providers[key] for key in sorted(providers)),
            config_path=self.config_path,
        )

    def get_provider(
        self,
        provider_id: str,
        *,
        include_builtin: bool = True,
    ) -> ProviderConfig | None:
        providers = {
            provider.provider_id: provider
            for provider in self.snapshot(include_builtin=include_builtin).providers
        }
        return providers.get(provider_id)

    def default_provider(self) -> ProviderConfig:
        snapshot = self.snapshot()
        providers = {provider.provider_id: provider for provider in snapshot.providers}
        return providers.get(snapshot.default_provider_id) or _builtin_faux_provider()

    def resolve_runtime_options(
        self,
        runtime_options: ProviderRuntimeOptions,
    ) -> ProviderRuntimeOptions:
        snapshot = self.snapshot()
        providers = {provider.provider_id: provider for provider in snapshot.providers}

        explicit_configured_provider = False
        selected_id = snapshot.default_provider_id
        if runtime_options.provider_id is not None and runtime_options.provider_id in providers:
            explicit_configured_provider = True
            selected_id = runtime_options.provider_id
        selected = providers.get(selected_id) or _builtin_faux_provider()
        if not selected.enabled:
            raise ConfigError(f"provider is disabled: {selected.provider_id}")

        base_options = _runtime_options_from_config(selected)
        if (
            runtime_options.provider is not None
            and runtime_options.provider != base_options.provider
            and not explicit_configured_provider
        ):
            base_options = ProviderRuntimeOptions(provider=runtime_options.provider)
        resolved = _merge_runtime_options(base_options, runtime_options)
        if resolved.provider_id is None:
            resolved = _merge_runtime_options(
                resolved,
                ProviderRuntimeOptions(provider_id=selected.provider_id),
            )
        return resolved

    def upsert_provider(
        self,
        provider_id: str,
        *,
        provider_type: ProviderKind | None = None,
        default_model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float | None = None,
        enabled: bool | None = None,
        make_default: bool = False,
    ) -> ProviderConfig:
        provider_id = provider_id.strip()
        if not provider_id:
            raise ConfigError("provider id is required")

        raw = self.load_raw()
        configured = self._configured_providers(raw)
        current = configured.get(provider_id)
        if current is None:
            current = ProviderConfig(
                provider_id=provider_id,
                provider_type=provider_type or "openai-compatible",
            )

        credential_ref = current.credential_ref
        if api_key_env is not None:
            credential_ref = CredentialRef.parse(f"env:{api_key_env}")
        elif api_key is not None:
            credential_ref = CredentialRef.parse(api_key)

        updated = current.model_copy(
            update={
                "provider_type": provider_type or current.provider_type,
                "default_model": default_model
                if default_model is not None
                else current.default_model,
                "base_url": base_url if base_url is not None else current.base_url,
                "credential_ref": credential_ref,
                "timeout_seconds": timeout_seconds
                if timeout_seconds is not None
                else current.timeout_seconds,
                "enabled": enabled if enabled is not None else current.enabled,
            }
        )
        updated = _normalize_provider_config(updated)
        _validate_provider_config(updated)

        configured[provider_id] = updated
        default_provider_id = (
            provider_id if make_default else self._default_provider_id(raw, configured)
        )
        self._save_configured_providers(
            raw,
            configured=configured,
            default_provider_id=default_provider_id,
        )
        return updated

    def set_default_provider(self, provider_id: str) -> ProviderConfig:
        provider = self.get_provider(provider_id)
        if provider is None:
            raise ConfigError(f"provider not found: {provider_id}")
        if not provider.enabled:
            raise ConfigError(f"provider is disabled: {provider_id}")

        raw = self.load_raw()
        configured = self._configured_providers(raw)
        self._save_configured_providers(
            raw,
            configured=configured,
            default_provider_id=provider_id,
        )
        return provider

    def remove_provider(self, provider_id: str) -> None:
        raw = self.load_raw()
        configured = self._configured_providers(raw)
        if provider_id not in configured:
            if provider_id == BUILTIN_FAUX_PROVIDER_ID:
                raise ConfigError("built-in provider cannot be removed: faux")
            raise ConfigError(f"provider not found: {provider_id}")

        del configured[provider_id]
        default_provider_id = self._default_provider_id(raw, configured)
        if default_provider_id == provider_id:
            default_provider_id = BUILTIN_FAUX_PROVIDER_ID
        self._save_configured_providers(
            raw,
            configured=configured,
            default_provider_id=default_provider_id,
        )

    def load_raw(self) -> dict[str, Any]:
        return self._file.read()

    def _configured_providers(self, raw: Mapping[str, Any]) -> dict[str, ProviderConfig]:
        providers: dict[str, ProviderConfig] = {}
        providers_section = raw.get("providers")
        if isinstance(providers_section, Mapping):
            items = providers_section.get("items") or {}
            if isinstance(items, Mapping):
                for provider_id, item in items.items():
                    if not isinstance(provider_id, str):
                        continue
                    if not isinstance(item, Mapping):
                        raise ConfigError(f"provider config must be an object: {provider_id}")
                    provider = _provider_config_from_mapping(provider_id, item)
                    providers[provider.provider_id] = provider
            elif isinstance(items, list):
                for item in items:
                    if not isinstance(item, Mapping):
                        raise ConfigError("provider config list entries must be objects")
                    provider_id = str(item.get("provider_id") or "").strip()
                    if not provider_id:
                        raise ConfigError("provider config entry is missing provider_id")
                    provider = _provider_config_from_mapping(provider_id, item)
                    providers[provider.provider_id] = provider

        legacy_provider = raw.get("provider")
        if isinstance(legacy_provider, Mapping):
            provider = _provider_config_from_legacy(legacy_provider)
            providers.setdefault(provider.provider_id, provider)
        elif legacy_provider is not None:
            raise ConfigError("provider config must be an object")

        return providers

    def _default_provider_id(
        self,
        raw: Mapping[str, Any],
        configured: Mapping[str, ProviderConfig],
    ) -> str:
        providers_section = raw.get("providers")
        if isinstance(providers_section, Mapping):
            default_id = providers_section.get("default")
            if isinstance(default_id, str) and default_id.strip():
                return default_id.strip()

        legacy_provider = raw.get("provider")
        if isinstance(legacy_provider, Mapping):
            provider_id = legacy_provider.get("provider_id")
            if isinstance(provider_id, str) and provider_id.strip():
                return provider_id.strip()

        if configured:
            return sorted(configured)[0]
        return BUILTIN_FAUX_PROVIDER_ID

    def _save_configured_providers(
        self,
        raw: Mapping[str, Any],
        *,
        configured: Mapping[str, ProviderConfig],
        default_provider_id: str,
    ) -> None:
        next_raw = deepcopy(dict(raw))
        next_raw.pop("provider", None)
        next_raw["providers"] = {
            "default": default_provider_id,
            "items": {
                provider_id: _provider_config_to_raw(config)
                for provider_id, config in sorted(configured.items())
            },
        }
        self._write_raw(next_raw)

    def _write_raw(self, raw: Mapping[str, Any]) -> None:
        self._file.write(raw)


def provider_config_to_public_dict(
    provider: ProviderConfig,
    *,
    is_default: bool = False,
) -> dict[str, Any]:
    return {
        "provider_id": provider.provider_id,
        "kind": provider.provider_type,
        "model": provider.default_model,
        "base_url": provider.base_url,
        "credential": provider.credential_ref.display(),
        "enabled": provider.enabled,
        "timeout": provider.timeout_seconds,
        "default": is_default,
    }


def _provider_config_from_mapping(
    provider_id: str,
    raw: Mapping[str, Any],
) -> ProviderConfig:
    provider_type = raw.get("provider_type") or raw.get("kind") or raw.get("provider")
    credential_ref = _credential_ref_from_mapping(raw)
    configured_provider_id = _optional_str(raw.get("provider_id")) or provider_id
    if configured_provider_id != provider_id:
        raise ConfigError(
            "provider_id must match providers.items key "
            f"({configured_provider_id} != {provider_id})"
        )
    provider = ProviderConfig(
        provider_id=configured_provider_id,
        provider_type=_normalize_provider_type(provider_type or "openai-compatible"),
        base_url=_optional_str(raw.get("base_url")),
        credential_ref=credential_ref,
        default_model=_optional_str(raw.get("default_model") or raw.get("model")),
        enabled=bool(raw.get("enabled", True)),
        timeout_seconds=float(raw.get("timeout_seconds") or raw.get("timeout") or 60.0),
        retry_policy=dict(raw.get("retry_policy") or {}),
        extra=dict(raw.get("extra") or {}),
    )
    return _normalize_provider_config(provider)


def _provider_config_from_legacy(raw: Mapping[str, Any]) -> ProviderConfig:
    kind = raw.get("kind") or raw.get("provider") or "faux"
    provider_type = _normalize_provider_type(kind)
    provider_id = _optional_str(raw.get("provider_id"))
    if provider_id is None:
        provider_id = BUILTIN_FAUX_PROVIDER_ID if provider_type == "faux" else "openai_compatible"
    provider = ProviderConfig(
        provider_id=provider_id,
        provider_type=provider_type,
        base_url=_optional_str(raw.get("base_url")),
        credential_ref=_credential_ref_from_mapping(raw),
        default_model=_optional_str(raw.get("model") or raw.get("default_model")),
        enabled=bool(raw.get("enabled", True)),
        timeout_seconds=float(raw.get("timeout") or raw.get("timeout_seconds") or 60.0),
    )
    return _normalize_provider_config(provider)


def _provider_config_to_raw(provider: ProviderConfig) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "provider_type": provider.provider_type,
        "provider_id": provider.provider_id,
        "enabled": provider.enabled,
        "timeout_seconds": provider.timeout_seconds,
    }
    if provider.default_model:
        raw["default_model"] = provider.default_model
    if provider.base_url:
        raw["base_url"] = provider.base_url
    if provider.credential_ref.type != "none":
        raw["credential_ref"] = _credential_ref_to_raw(provider.credential_ref)
    if provider.retry_policy:
        raw["retry_policy"] = provider.retry_policy
    if provider.extra:
        raw["extra"] = provider.extra
    return raw


def _runtime_options_from_config(provider: ProviderConfig) -> ProviderRuntimeOptions:
    provider_type = _normalize_provider_type(provider.provider_type)
    if provider_type == "faux":
        return ProviderRuntimeOptions(
            provider="faux",
            provider_id=provider.provider_id,
            model=provider.default_model or "faux",
            timeout=provider.timeout_seconds,
        )
    return ProviderRuntimeOptions(
        provider="openai-compatible",
        provider_id=provider.provider_id,
        model=provider.default_model,
        base_url=provider.base_url,
        credential_ref=provider.credential_ref,
        timeout=provider.timeout_seconds,
    )


def _merge_runtime_options(
    base: ProviderRuntimeOptions,
    override: ProviderRuntimeOptions,
) -> ProviderRuntimeOptions:
    return ProviderRuntimeOptions(
        provider=override.provider if override.provider is not None else base.provider,
        provider_id=override.provider_id if override.provider_id is not None else base.provider_id,
        model=override.model if override.model is not None else base.model,
        base_url=override.base_url if override.base_url is not None else base.base_url,
        api_key=override.api_key if override.api_key is not None else base.api_key,
        api_key_env=override.api_key_env if override.api_key_env is not None else base.api_key_env,
        credential_ref=override.credential_ref
        if override.credential_ref is not None
        else base.credential_ref,
        timeout=override.timeout if override.timeout is not None else base.timeout,
    )


def _normalize_provider_config(provider: ProviderConfig) -> ProviderConfig:
    provider_type = _normalize_provider_type(provider.provider_type)
    default_model = provider.default_model
    if provider_type == "faux" and not default_model:
        default_model = "faux"
    return provider.model_copy(
        update={
            "provider_type": provider_type,
            "default_model": default_model,
        }
    )


def _validate_provider_config(provider: ProviderConfig) -> None:
    if provider.provider_type == "openai-compatible":
        if not provider.base_url:
            raise ConfigError("--base-url is required for openai-compatible provider")
        if not provider.default_model:
            raise ConfigError("--model is required for openai-compatible provider")


def _normalize_provider_type(value: object) -> ProviderKind:
    if not isinstance(value, str):
        raise ConfigError(f"unsupported provider kind: {value}")
    if value in {"openai-compatible", "openai_compatible"}:
        return "openai-compatible"
    if value == "faux":
        return "faux"
    raise ConfigError(f"unsupported provider kind: {value}")


def _credential_ref_from_mapping(raw: Mapping[str, Any]) -> CredentialRef:
    credential_ref = raw.get("credential_ref")
    if isinstance(credential_ref, str) or credential_ref is None:
        if credential_ref is not None:
            return CredentialRef.parse(credential_ref)
    elif isinstance(credential_ref, Mapping):
        return CredentialRef.model_validate(dict(credential_ref))
    if credential_ref is not None:
        return CredentialRef.model_validate(credential_ref)
    api_key_env = raw.get("api_key_env")
    if isinstance(api_key_env, str) and api_key_env:
        return CredentialRef.parse(f"env:{api_key_env}")
    api_key = raw.get("api_key")
    if isinstance(api_key, str) and api_key:
        return CredentialRef.parse(api_key)
    return CredentialRef()


def _credential_ref_to_raw(ref: CredentialRef) -> str | dict[str, str]:
    if ref.type == "env":
        return f"env:{ref.name}"
    if ref.type == "inline":
        return {"type": "inline", "value": ref.value or ""}
    return "none"


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _builtin_faux_provider() -> ProviderConfig:
    return ProviderConfig(
        provider_id=BUILTIN_FAUX_PROVIDER_ID,
        provider_type="faux",
        default_model="faux",
        enabled=True,
        timeout_seconds=60.0,
    )
