from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from ouro.config.credentials import CredentialRef
from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.file import JsonConfigFile, atomic_config_update
from ouro.config.provider_codec import (
    provider_config_from_mapping,
    provider_config_to_raw,
)
from ouro.config.provider_models import (
    BUILTIN_FAUX_PROVIDER_ID,
    ProviderConfig,
    ProviderConfigSnapshot,
    ProviderProtocol,
    ProviderRuntimeOptions,
    ProviderType,
    builtin_faux_provider,
    resolve_provider_protocol,
)
from ouro.config.provider_runtime import merge_runtime_options, runtime_options_from_config
from ouro.config.provider_templates import ProviderTemplate
from ouro.config.reasoning import EndpointReasoningConfig
from ouro.core.errors import ConfigError
from ouro.core.provider import ProviderRetryPolicy


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
            and runtime_options.protocol is None
        ):
            raise ConfigError(f"provider not found: {requested_id}")

        selected_id = (
            requested_id
            if requested_is_configured and requested_id is not None
            else snapshot.default_provider_id
        )
        selected = providers.get(selected_id) or builtin_faux_provider()
        if (
            requested_is_configured
            and runtime_options.protocol is not None
            and runtime_options.protocol != selected.protocol
        ):
            raise ConfigError(
                f"protocol does not match configured provider {selected.provider_id}: "
                f"{runtime_options.protocol} != {selected.protocol}"
            )

        ad_hoc_provider = (requested_id is not None and not requested_is_configured) or (
            requested_id is None
            and runtime_options.protocol is not None
            and runtime_options.protocol != selected.protocol
        )
        if ad_hoc_provider:
            if runtime_options.endpoint_id is not None:
                raise ConfigError("--endpoint requires a configured provider")
            base_options = ProviderRuntimeOptions(protocol=runtime_options.protocol)
        else:
            if not selected.enabled:
                raise ConfigError(f"provider is disabled: {selected.provider_id}")
            base_options = runtime_options_from_config(
                selected,
                endpoint_id=runtime_options.endpoint_id,
            )
        return merge_runtime_options(base_options, runtime_options)

    @atomic_config_update
    def create_provider(
        self,
        provider_id: str,
        *,
        provider_type: ProviderType,
        protocol: ProviderProtocol | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        enabled: bool = True,
    ) -> ProviderConfig:
        """Create a new empty provider and fail if its id already exists."""

        provider_id = self._validate_mutable_provider_id(provider_id)
        self._reject_faux_type(provider_type)
        raw = self.load_raw()
        configured = self._configured_providers(raw)
        if provider_id in configured:
            raise ConfigError(
                f"provider already exists: {provider_id}; use provider update or provider replace"
            )
        try:
            provider = ProviderConfig(
                provider_id=provider_id,
                provider_type=provider_type,
                protocol=resolve_provider_protocol(provider_type, protocol),
                credential_ref=self._updated_credential(
                    CredentialRef(), api_key=api_key, api_key_env=api_key_env
                ),
                base_url=base_url,
                api_path=api_path,
                timeout_seconds=timeout_seconds if timeout_seconds is not None else 60.0,
                retry_policy=retry_policy or ProviderRetryPolicy(),
                enabled=enabled,
            )
        except (ValueError, ValidationError) as exc:
            raise ConfigError(f"invalid provider config: {exc}") from exc
        self._store_provider(raw, configured=configured, provider=provider, make_default=False)
        return provider

    @atomic_config_update
    def update_provider(
        self,
        provider_id: str,
        *,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        enabled: bool | None = None,
        make_default: bool = False,
    ) -> ProviderConfig:
        """Update mutable settings without changing provider type or protocol."""

        provider_id = self._validate_mutable_provider_id(provider_id)
        raw = self.load_raw()
        configured = self._configured_providers(raw)
        current = configured.get(provider_id)
        if current is None:
            raise ConfigError(f"provider not found: {provider_id}")
        try:
            provider = ProviderConfig.model_validate(
                current.model_copy(
                    update={
                        "credential_ref": self._updated_credential(
                            current.credential_ref,
                            api_key=api_key,
                            api_key_env=api_key_env,
                        ),
                        "base_url": base_url if base_url is not None else current.base_url,
                        "api_path": api_path if api_path is not None else current.api_path,
                        "timeout_seconds": (
                            timeout_seconds
                            if timeout_seconds is not None
                            else current.timeout_seconds
                        ),
                        "retry_policy": retry_policy or current.retry_policy,
                        "enabled": enabled if enabled is not None else current.enabled,
                    }
                ).model_dump()
            )
        except ValidationError as exc:
            raise ConfigError(f"invalid provider config: {exc}") from exc
        self._store_provider(
            raw, configured=configured, provider=provider, make_default=make_default
        )
        return provider

    @atomic_config_update
    def replace_provider(
        self,
        provider_id: str,
        *,
        provider_type: ProviderType,
        protocol: ProviderProtocol | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        enabled: bool = True,
    ) -> ProviderConfig:
        """Replace an existing provider and discard its endpoint registry."""

        provider_id = self._validate_mutable_provider_id(provider_id)
        self._reject_faux_type(provider_type)
        raw = self.load_raw()
        configured = self._configured_providers(raw)
        if provider_id not in configured:
            raise ConfigError(f"provider not found: {provider_id}")
        try:
            provider = ProviderConfig(
                provider_id=provider_id,
                provider_type=provider_type,
                protocol=resolve_provider_protocol(provider_type, protocol),
                credential_ref=self._updated_credential(
                    CredentialRef(), api_key=api_key, api_key_env=api_key_env
                ),
                base_url=base_url,
                api_path=api_path,
                timeout_seconds=timeout_seconds if timeout_seconds is not None else 60.0,
                retry_policy=retry_policy or ProviderRetryPolicy(),
                enabled=enabled,
            )
        except (ValueError, ValidationError) as exc:
            raise ConfigError(f"invalid provider config: {exc}") from exc
        self._store_provider(raw, configured=configured, provider=provider, make_default=False)
        return provider

    @atomic_config_update
    def create_from_template(
        self,
        template: ProviderTemplate,
        *,
        provider_id: str | None = None,
        endpoint_id: str | None = None,
        model_id: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        enabled: bool = True,
        make_default: bool = False,
    ) -> ProviderConfig:
        return self._configure_template(
            template,
            operation="create",
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            model_id=model_id,
            base_url=base_url,
            api_path=api_path,
            api_key=api_key,
            api_key_env=api_key_env,
            timeout_seconds=timeout_seconds,
            retry_policy=retry_policy,
            enabled=enabled,
            make_default=make_default,
        )

    @atomic_config_update
    def update_from_template(
        self,
        template: ProviderTemplate,
        *,
        provider_id: str | None = None,
        endpoint_id: str | None = None,
        model_id: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        enabled: bool | None = None,
        make_default: bool = False,
    ) -> ProviderConfig:
        return self._configure_template(
            template,
            operation="update",
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            model_id=model_id,
            base_url=base_url,
            api_path=api_path,
            api_key=api_key,
            api_key_env=api_key_env,
            timeout_seconds=timeout_seconds,
            retry_policy=retry_policy,
            enabled=enabled,
            make_default=make_default,
        )

    @atomic_config_update
    def replace_from_template(
        self,
        template: ProviderTemplate,
        *,
        provider_id: str | None = None,
        endpoint_id: str | None = None,
        model_id: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        enabled: bool = True,
        make_default: bool = False,
    ) -> ProviderConfig:
        return self._configure_template(
            template,
            operation="replace",
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            model_id=model_id,
            base_url=base_url,
            api_path=api_path,
            api_key=api_key,
            api_key_env=api_key_env,
            timeout_seconds=timeout_seconds,
            retry_policy=retry_policy,
            enabled=enabled,
            make_default=make_default,
        )

    def _configure_template(
        self,
        template: ProviderTemplate,
        *,
        operation: Literal["create", "update", "replace"],
        provider_id: str | None = None,
        endpoint_id: str | None = None,
        model_id: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        enabled: bool | None = None,
        make_default: bool = False,
    ) -> ProviderConfig:
        """Apply all template endpoints in one validated config transaction."""

        target_provider_id = self._validate_mutable_provider_id(
            provider_id if provider_id is not None else template.provider_id
        )
        target_endpoint_id = (
            endpoint_id if endpoint_id is not None else template.default_endpoint
        ).strip()
        if not target_endpoint_id:
            raise ConfigError("endpoint id is required")
        template_endpoint_ids = {
            item.endpoint_id
            for item in template.endpoints
            if item.endpoint_id != template.default_endpoint
        }
        if target_endpoint_id in template_endpoint_ids:
            raise ConfigError(
                "default endpoint override conflicts with another template endpoint: "
                f"{target_endpoint_id}"
            )

        raw = self.load_raw()
        configured = self._configured_providers(raw)
        current = configured.get(target_provider_id)
        if operation == "create" and current is not None:
            raise ConfigError(
                f"provider already exists: {target_provider_id}; "
                "use provider update or provider replace"
            )
        if operation != "create" and current is None:
            raise ConfigError(f"provider not found: {target_provider_id}")
        if (
            operation == "update"
            and current is not None
            and (
                current.provider_type != template.provider_type
                or current.protocol != template.protocol
            )
        ):
            raise ConfigError(
                f"provider type/protocol does not match template {template.template_id}: "
                f"{current.provider_type}/{current.protocol} != "
                f"{template.provider_type}/{template.protocol}"
            )

        preserve_current = operation == "update" and current is not None
        credential_ref = (
            current.credential_ref if preserve_current and current is not None else CredentialRef()
        )
        credential_ref = self._updated_credential(
            credential_ref, api_key=api_key, api_key_env=api_key_env
        )
        if credential_ref.type == "none":
            credential_ref = CredentialRef.parse(f"env:{template.credential_env}")

        try:
            endpoints = (
                {item.endpoint_id: item for item in current.endpoints}
                if preserve_current and current is not None
                else {}
            )
            for template_endpoint in template.endpoints:
                is_default = template_endpoint.endpoint_id == template.default_endpoint
                configured_endpoint_id = (
                    target_endpoint_id if is_default else template_endpoint.endpoint_id
                )
                configured_model_id = (
                    model_id if is_default and model_id is not None else template_endpoint.model_id
                )
                endpoints[configured_endpoint_id] = ProviderEndpointConfig.model_validate(
                    template_endpoint.model_copy(
                        update={
                            "endpoint_id": configured_endpoint_id,
                            "model_id": configured_model_id,
                        }
                    ).model_dump()
                )
            provider = ProviderConfig(
                provider_id=target_provider_id,
                provider_type=template.provider_type,
                protocol=template.protocol,
                credential_ref=credential_ref,
                default_endpoint=target_endpoint_id,
                endpoints=tuple(endpoints[key] for key in sorted(endpoints)),
                base_url=base_url if base_url is not None else template.base_url,
                api_path=api_path if api_path is not None else template.api_path,
                enabled=(
                    enabled
                    if enabled is not None
                    else current.enabled
                    if preserve_current and current is not None
                    else True
                ),
                timeout_seconds=(
                    timeout_seconds
                    if timeout_seconds is not None
                    else current.timeout_seconds
                    if preserve_current and current is not None
                    else 60.0
                ),
                retry_policy=(
                    retry_policy
                    or (
                        current.retry_policy
                        if preserve_current and current is not None
                        else ProviderRetryPolicy()
                    )
                ),
                extra=current.extra if preserve_current and current is not None else {},
            )
        except ValidationError as exc:
            raise ConfigError(f"invalid provider template config: {exc}") from exc
        self._store_provider(
            raw, configured=configured, provider=provider, make_default=make_default
        )
        return provider

    @atomic_config_update
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
        reasoning: EndpointReasoningConfig | None = None,
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
                    "reasoning": reasoning if reasoning is not None else current.reasoning,
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

    @atomic_config_update
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

    @atomic_config_update
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

    @atomic_config_update
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

    @atomic_config_update
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

    @staticmethod
    def _validate_mutable_provider_id(provider_id: str) -> str:
        normalized = provider_id.strip()
        if not normalized:
            raise ConfigError("provider id is required")
        if normalized == BUILTIN_FAUX_PROVIDER_ID:
            raise ConfigError("built-in provider cannot be modified: faux")
        return normalized

    @staticmethod
    def _reject_faux_type(provider_type: ProviderType) -> None:
        if provider_type == "faux":
            raise ConfigError("provider type is reserved for the built-in Faux provider: faux")

    @staticmethod
    def _updated_credential(
        current: CredentialRef,
        *,
        api_key: str | None,
        api_key_env: str | None,
    ) -> CredentialRef:
        if api_key is not None and api_key_env is not None:
            raise ConfigError("api_key and api_key_env cannot be combined")
        if api_key_env is not None:
            normalized = api_key_env.strip()
            if not normalized:
                raise ConfigError("api key environment variable is required")
            return CredentialRef.parse(f"env:{normalized}")
        if api_key is not None:
            if not api_key:
                raise ConfigError("api key is required")
            return CredentialRef.parse(api_key)
        return current

    def _store_provider(
        self,
        raw: Mapping[str, Any],
        *,
        configured: dict[str, ProviderConfig],
        provider: ProviderConfig,
        make_default: bool,
    ) -> None:
        previous_default_provider_id = self._default_provider_id(raw, configured)
        if make_default and (not provider.enabled or provider.endpoint() is None):
            raise ConfigError("a default provider must be enabled and have a default endpoint")
        configured[provider.provider_id] = provider
        if make_default:
            default_provider_id = provider.provider_id
        elif previous_default_provider_id == provider.provider_id and (
            not provider.enabled or provider.endpoint() is None
        ):
            default_provider_id = BUILTIN_FAUX_PROVIDER_ID
        else:
            default_provider_id = previous_default_provider_id
        self._save_configured_providers(
            raw,
            configured=configured,
            default_provider_id=default_provider_id,
        )

    def load_raw(self) -> dict[str, Any]:
        return self._file.read()

    def _configured_providers(self, raw: Mapping[str, Any]) -> dict[str, ProviderConfig]:
        providers: dict[str, ProviderConfig] = {}
        if "provider" in raw:
            raise ConfigError("unsupported root config field provider; use providers")
        providers_section = raw.get("providers")
        if providers_section is not None and not isinstance(providers_section, Mapping):
            raise ConfigError("providers config must be an object")
        if isinstance(providers_section, Mapping):
            unsupported = sorted(set(providers_section) - {"default", "items"})
            if unsupported:
                raise ConfigError(f"unsupported providers config fields: {', '.join(unsupported)}")
            items = providers_section.get("items", {})
            if isinstance(items, Mapping):
                for provider_id, item in items.items():
                    if not isinstance(provider_id, str):
                        raise ConfigError("provider ids must be strings")
                    if not isinstance(item, Mapping):
                        raise ConfigError(f"provider config must be an object: {provider_id}")
                    provider = provider_config_from_mapping(provider_id, item)
                    self._reject_reserved_provider(provider)
                    providers[provider.provider_id] = provider
            else:
                raise ConfigError("providers.items must be an object")

        return providers

    @staticmethod
    def _reject_reserved_provider(provider: ProviderConfig) -> None:
        if provider.provider_id == BUILTIN_FAUX_PROVIDER_ID:
            raise ConfigError("provider id is reserved for the built-in Faux provider: faux")
        if provider.provider_type == "faux" or provider.protocol == "faux":
            raise ConfigError("provider type is reserved for the built-in Faux provider: faux")

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
        next_raw["providers"] = {
            "default": default_provider_id,
            "items": {
                provider_id: provider_config_to_raw(config)
                for provider_id, config in sorted(configured.items())
            },
        }
        self._file.write(next_raw)
