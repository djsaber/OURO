from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ouro.config.credentials import CredentialRef
from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.file import JsonConfigFile
from ouro.config.provider_codec import (
    provider_config_from_legacy,
    provider_config_from_mapping,
    provider_config_to_raw,
)
from ouro.config.provider_models import (
    BUILTIN_FAUX_PROVIDER_ID,
    ProviderConfig,
    ProviderConfigSnapshot,
    ProviderKind,
    ProviderRuntimeOptions,
    builtin_faux_provider,
    normalize_provider_config,
)
from ouro.config.provider_runtime import merge_runtime_options, runtime_options_from_config
from ouro.core.errors import ConfigError
from ouro.core.provider import ThinkingLevel


class ProviderConfigManager:
    def __init__(self, root: str | Path | None = None) -> None:
        self._file = JsonConfigFile(root)
        self.root = self._file.root
        self.config_path = self._file.path

    def snapshot(self, *, include_builtin: bool = True) -> ProviderConfigSnapshot:
        raw = self.load_raw()
        configured = self._configured_providers(raw)
        default_provider_id = self._default_provider_id(raw, configured)
        if (
            default_provider_id != BUILTIN_FAUX_PROVIDER_ID
            and default_provider_id not in configured
        ):
            raise ConfigError(f"default provider not found: {default_provider_id}")
        providers = dict(configured)
        if include_builtin:
            providers.setdefault(BUILTIN_FAUX_PROVIDER_ID, builtin_faux_provider())
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

    def get_endpoint(
        self,
        provider_id: str,
        endpoint_id: str | None = None,
    ) -> ProviderEndpointConfig | None:
        provider = self.get_provider(provider_id)
        return provider.endpoint(endpoint_id) if provider is not None else None

    def default_provider(self) -> ProviderConfig:
        snapshot = self.snapshot()
        providers = {provider.provider_id: provider for provider in snapshot.providers}
        return providers.get(snapshot.default_provider_id) or builtin_faux_provider()

    def resolve_runtime_options(
        self,
        runtime_options: ProviderRuntimeOptions,
    ) -> ProviderRuntimeOptions:
        snapshot = self.snapshot()
        providers = {provider.provider_id: provider for provider in snapshot.providers}

        requested_id = runtime_options.provider_id
        requested_is_configured = requested_id is not None and requested_id in providers
        if (
            requested_id is not None
            and not requested_is_configured
            and runtime_options.provider is None
        ):
            raise ConfigError(f"provider not found: {requested_id}")

        selected_id = (
            requested_id
            if requested_is_configured and requested_id is not None
            else snapshot.default_provider_id
        )
        selected = providers.get(selected_id) or builtin_faux_provider()
        selected_kind = selected.provider_type
        if (
            requested_is_configured
            and runtime_options.provider is not None
            and runtime_options.provider != selected_kind
        ):
            raise ConfigError(
                f"provider kind does not match configured provider {selected.provider_id}: "
                f"{runtime_options.provider} != {selected_kind}"
            )

        ad_hoc_provider = (requested_id is not None and not requested_is_configured) or (
            requested_id is None
            and runtime_options.provider is not None
            and runtime_options.provider != selected_kind
        )
        if ad_hoc_provider:
            if runtime_options.endpoint_id is not None:
                raise ConfigError("--endpoint requires a configured provider")
            base_options = ProviderRuntimeOptions(provider=runtime_options.provider)
        else:
            if not selected.enabled:
                raise ConfigError(f"provider is disabled: {selected.provider_id}")
            base_options = runtime_options_from_config(
                selected,
                endpoint_id=runtime_options.endpoint_id,
            )
        return merge_runtime_options(base_options, runtime_options)

    def upsert_provider(
        self,
        provider_id: str,
        *,
        provider_type: ProviderKind | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float | None = None,
        enabled: bool | None = None,
        make_default: bool = False,
        default_endpoint_id: str | None = None,
        default_endpoint_model: str | None = None,
        default_endpoint_base_url: str | None = None,
        default_endpoint_api_path: str | None = None,
    ) -> ProviderConfig:
        """Create or update a provider.

        Provider-level ``base_url`` and ``api_path`` act as defaults for the
        endpoints owned by this provider. An optional default endpoint can be
        created in the same call by supplying ``default_endpoint_id`` and its
        configuration; when provided, ``default_endpoint_id`` is written to the
        provider's ``default_endpoint`` field.
        """

        provider_id = provider_id.strip()
        if not provider_id:
            raise ConfigError("provider id is required")
        if provider_id == BUILTIN_FAUX_PROVIDER_ID:
            raise ConfigError("built-in provider cannot be modified: faux")

        raw = self.load_raw()
        configured = self._configured_providers(raw)
        previous_default_provider_id = self._default_provider_id(raw, configured)
        try:
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
                    "credential_ref": credential_ref,
                    "base_url": base_url if base_url is not None else current.base_url,
                    "api_path": api_path if api_path is not None else current.api_path,
                    "timeout_seconds": timeout_seconds
                    if timeout_seconds is not None
                    else current.timeout_seconds,
                    "enabled": enabled if enabled is not None else current.enabled,
                }
            )
            updated = normalize_provider_config(updated)
            if default_endpoint_id is not None:
                updated = self._apply_default_endpoint(
                    updated,
                    endpoint_id=default_endpoint_id,
                    model_id=default_endpoint_model,
                    base_url=default_endpoint_base_url,
                    api_path=default_endpoint_api_path,
                )
            elif (
                default_endpoint_model is not None
                or default_endpoint_base_url is not None
                or default_endpoint_api_path is not None
            ):
                raise ConfigError("--endpoint is required when configuring a default endpoint")
            updated = ProviderConfig.model_validate(updated.model_dump())
        except ValidationError as exc:
            raise ConfigError(f"invalid provider config: {exc}") from exc
        if make_default and (not updated.enabled or updated.endpoint() is None):
            raise ConfigError("a default provider must be enabled and have a default endpoint")

        configured[provider_id] = updated
        if make_default:
            default_provider_id = provider_id
        elif previous_default_provider_id == provider_id and not updated.enabled:
            default_provider_id = BUILTIN_FAUX_PROVIDER_ID
        else:
            default_provider_id = previous_default_provider_id
        self._save_configured_providers(
            raw,
            configured=configured,
            default_provider_id=default_provider_id,
        )
        return updated

    def _apply_default_endpoint(
        self,
        provider: ProviderConfig,
        *,
        endpoint_id: str,
        model_id: str | None,
        base_url: str | None,
        api_path: str | None,
    ) -> ProviderConfig:
        endpoint_id = endpoint_id.strip()
        if not endpoint_id:
            raise ConfigError("default endpoint id is required")

        current = provider.endpoint(endpoint_id)
        if current is None:
            if model_id is None:
                raise ConfigError("--model is required for a new default endpoint")
            current = ProviderEndpointConfig(
                endpoint_id=endpoint_id,
                model_id=model_id,
                base_url=base_url,
                api_path=api_path,
            )
        else:
            current = current.model_copy(
                update={
                    "model_id": model_id if model_id is not None else current.model_id,
                    "base_url": base_url if base_url is not None else current.base_url,
                    "api_path": api_path if api_path is not None else current.api_path,
                }
            )

        endpoints = {endpoint.endpoint_id: endpoint for endpoint in provider.endpoints}
        endpoints[endpoint_id] = current
        return provider.model_copy(
            update={
                "endpoints": tuple(endpoints[key] for key in sorted(endpoints)),
                "default_endpoint": endpoint_id,
            }
        )

    def upsert_endpoint(
        self,
        provider_id: str,
        endpoint_id: str,
        *,
        model_id: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        temperature: float | None = None,
        context_window: int | None = None,
        max_output_tokens: int | None = None,
        supports_tools: bool | None = None,
        supports_vision: bool | None = None,
        supports_streaming: bool | None = None,
        supports_structured_output: bool | None = None,
        supports_reasoning: bool | None = None,
        supported_thinking_levels: Sequence[ThinkingLevel] | None = None,
        default_thinking_level: ThinkingLevel | None = None,
        request_options: Mapping[str, Any] | None = None,
        make_default: bool = False,
    ) -> ProviderEndpointConfig:
        provider_id = provider_id.strip()
        endpoint_id = endpoint_id.strip()
        if not endpoint_id:
            raise ConfigError("endpoint id is required")

        raw = self.load_raw()
        configured = self._configured_providers(raw)
        provider = configured.get(provider_id)
        if provider is None:
            raise ConfigError(f"provider not found: {provider_id}")

        try:
            current = provider.endpoint(endpoint_id)
            if current is None:
                if model_id is None:
                    raise ConfigError("--model is required for a new endpoint")
                current = ProviderEndpointConfig(
                    endpoint_id=endpoint_id,
                    model_id=model_id,
                    base_url=base_url,
                )
        except ValidationError as exc:
            raise ConfigError(f"invalid endpoint config: {exc}") from exc

        next_supports_reasoning = (
            supports_reasoning if supports_reasoning is not None else current.supports_reasoning
        )
        next_levels = (
            tuple(supported_thinking_levels)
            if supported_thinking_levels is not None
            else current.supported_thinking_levels
        )
        next_default_level = (
            default_thinking_level
            if default_thinking_level is not None
            else current.default_thinking_level
        )
        if not next_supports_reasoning:
            next_levels = ()
            next_default_level = None

        try:
            updated = current.model_copy(
                update={
                    "model_id": model_id if model_id is not None else current.model_id,
                    "base_url": base_url if base_url is not None else current.base_url,
                    "api_path": api_path if api_path is not None else current.api_path,
                    "temperature": temperature if temperature is not None else current.temperature,
                    "context_window": context_window
                    if context_window is not None
                    else current.context_window,
                    "max_output_tokens": max_output_tokens
                    if max_output_tokens is not None
                    else current.max_output_tokens,
                    "supports_tools": supports_tools
                    if supports_tools is not None
                    else current.supports_tools,
                    "supports_vision": supports_vision
                    if supports_vision is not None
                    else current.supports_vision,
                    "supports_streaming": supports_streaming
                    if supports_streaming is not None
                    else current.supports_streaming,
                    "supports_structured_output": supports_structured_output
                    if supports_structured_output is not None
                    else current.supports_structured_output,
                    "supports_reasoning": next_supports_reasoning,
                    "supported_thinking_levels": next_levels,
                    "default_thinking_level": next_default_level,
                    "request_options": dict(request_options)
                    if request_options is not None
                    else current.request_options,
                }
            )
            updated = ProviderEndpointConfig.model_validate(updated.model_dump())
        except ValidationError as exc:
            raise ConfigError(f"invalid endpoint config: {exc}") from exc

        endpoints = {endpoint.endpoint_id: endpoint for endpoint in provider.endpoints}
        endpoints[endpoint_id] = updated
        default_endpoint = (
            endpoint_id
            if make_default or provider.default_endpoint is None
            else provider.default_endpoint
        )
        provider = provider.model_copy(
            update={
                "endpoints": tuple(endpoints[key] for key in sorted(endpoints)),
                "default_endpoint": default_endpoint,
            }
        )
        try:
            provider = ProviderConfig.model_validate(provider.model_dump())
        except ValidationError as exc:
            raise ConfigError(f"invalid provider config: {exc}") from exc
        configured[provider_id] = provider
        self._save_configured_providers(
            raw,
            configured=configured,
            default_provider_id=self._default_provider_id(raw, configured),
        )
        return updated

    def set_default_provider(self, provider_id: str) -> ProviderConfig:
        provider = self.get_provider(provider_id)
        if provider is None:
            raise ConfigError(f"provider not found: {provider_id}")
        if not provider.enabled:
            raise ConfigError(f"provider is disabled: {provider_id}")
        if provider.endpoint() is None:
            raise ConfigError(f"provider has no default endpoint: {provider_id}")

        raw = self.load_raw()
        configured = self._configured_providers(raw)
        self._save_configured_providers(
            raw,
            configured=configured,
            default_provider_id=provider_id,
        )
        return provider

    def set_default_endpoint(
        self,
        provider_id: str,
        endpoint_id: str,
    ) -> ProviderEndpointConfig:
        raw = self.load_raw()
        configured = self._configured_providers(raw)
        provider = configured.get(provider_id)
        if provider is None:
            raise ConfigError(f"provider not found: {provider_id}")
        endpoint = provider.endpoint(endpoint_id)
        if endpoint is None:
            raise ConfigError(f"endpoint not found: {provider_id}/{endpoint_id}")
        configured[provider_id] = provider.model_copy(update={"default_endpoint": endpoint_id})
        self._save_configured_providers(
            raw,
            configured=configured,
            default_provider_id=self._default_provider_id(raw, configured),
        )
        return endpoint

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

    def remove_endpoint(self, provider_id: str, endpoint_id: str) -> None:
        raw = self.load_raw()
        configured = self._configured_providers(raw)
        provider = configured.get(provider_id)
        if provider is None:
            raise ConfigError(f"provider not found: {provider_id}")
        endpoints = {endpoint.endpoint_id: endpoint for endpoint in provider.endpoints}
        if endpoint_id not in endpoints:
            raise ConfigError(f"endpoint not found: {provider_id}/{endpoint_id}")
        del endpoints[endpoint_id]
        default_endpoint = provider.default_endpoint
        if default_endpoint == endpoint_id:
            default_endpoint = None
        updated_provider = provider.model_copy(
            update={
                "endpoints": tuple(endpoints[key] for key in sorted(endpoints)),
                "default_endpoint": default_endpoint,
            }
        )
        configured[provider_id] = updated_provider
        default_provider_id = self._default_provider_id(raw, configured)
        if default_provider_id == provider_id and updated_provider.endpoint() is None:
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
        if providers_section is not None and not isinstance(providers_section, Mapping):
            raise ConfigError("providers config must be an object")
        if isinstance(providers_section, Mapping):
            items = providers_section.get("items", {})
            if isinstance(items, Mapping):
                for provider_id, item in items.items():
                    if not isinstance(provider_id, str):
                        continue
                    if not isinstance(item, Mapping):
                        raise ConfigError(f"provider config must be an object: {provider_id}")
                    provider = provider_config_from_mapping(provider_id, item)
                    self._reject_reserved_provider(provider)
                    providers[provider.provider_id] = provider
            elif isinstance(items, list):
                for item in items:
                    if not isinstance(item, Mapping):
                        raise ConfigError("provider config list entries must be objects")
                    provider_id = str(item.get("provider_id") or "").strip()
                    if not provider_id:
                        raise ConfigError("provider config entry is missing provider_id")
                    provider = provider_config_from_mapping(provider_id, item)
                    self._reject_reserved_provider(provider)
                    if provider.provider_id in providers:
                        raise ConfigError(f"duplicate provider id: {provider.provider_id}")
                    providers[provider.provider_id] = provider
            else:
                raise ConfigError("providers.items must be an object or list")

        legacy_provider = raw.get("provider")
        if isinstance(legacy_provider, Mapping):
            provider = provider_config_from_legacy(legacy_provider)
            if provider.provider_id != BUILTIN_FAUX_PROVIDER_ID:
                providers.setdefault(provider.provider_id, provider)
        elif legacy_provider is not None:
            raise ConfigError("provider config must be an object")

        return providers

    @staticmethod
    def _reject_reserved_provider(provider: ProviderConfig) -> None:
        if provider.provider_id == BUILTIN_FAUX_PROVIDER_ID:
            raise ConfigError("provider id is reserved for the built-in Faux provider: faux")

    def _default_provider_id(
        self,
        raw: Mapping[str, Any],
        configured: Mapping[str, ProviderConfig],
    ) -> str:
        providers_section = raw.get("providers")
        if isinstance(providers_section, Mapping):
            if "default" in providers_section:
                default_id = providers_section.get("default")
                if not isinstance(default_id, str) or not default_id.strip():
                    raise ConfigError("providers.default must be a non-empty string")
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
                provider_id: provider_config_to_raw(config)
                for provider_id, config in sorted(configured.items())
            },
        }
        self._file.write(next_raw)
