from __future__ import annotations

import hashlib
import json
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
    ProviderTemplateBinding,
    ProviderTemplateEndpointBinding,
    ProviderTemplateOrigin,
    ProviderTemplateUpdateResult,
    ProviderType,
    builtin_faux_provider,
    resolve_provider_connection,
)
from ouro.config.provider_runtime import merge_runtime_options, runtime_options_from_config
from ouro.config.provider_templates import ProviderTemplate
from ouro.config.reasoning import EndpointReasoningConfig
from ouro.core.base import new_id
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
            if runtime_options.auth_driver not in (None, "static"):
                raise ConfigError("managed authentication requires a configured provider")
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
        auth_driver: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        provider_options: Mapping[str, Any] | None = None,
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
            connection = resolve_provider_connection(provider_type, protocol, auth_driver)
            provider = ProviderConfig(
                provider_id=provider_id,
                provider_type=provider_type,
                protocol=connection.protocol,
                auth_driver=connection.auth_driver,
                credential_ref=self._updated_credential(
                    self._initial_credential(connection.auth_driver),
                    api_key=api_key,
                    api_key_env=api_key_env,
                ),
                base_url=base_url,
                api_path=api_path,
                timeout_seconds=timeout_seconds if timeout_seconds is not None else 60.0,
                retry_policy=retry_policy or ProviderRetryPolicy(),
                extra=dict(provider_options or {}),
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
        provider_options: Mapping[str, Any] | None = None,
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
                        "extra": (
                            dict(provider_options)
                            if provider_options is not None
                            else current.extra
                        ),
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
        auth_driver: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        provider_options: Mapping[str, Any] | None = None,
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
            connection = resolve_provider_connection(provider_type, protocol, auth_driver)
            provider = ProviderConfig(
                provider_id=provider_id,
                provider_type=provider_type,
                protocol=connection.protocol,
                auth_driver=connection.auth_driver,
                credential_ref=self._updated_credential(
                    self._initial_credential(connection.auth_driver),
                    api_key=api_key,
                    api_key_env=api_key_env,
                ),
                base_url=base_url,
                api_path=api_path,
                timeout_seconds=timeout_seconds if timeout_seconds is not None else 60.0,
                retry_policy=retry_policy or ProviderRetryPolicy(),
                extra=dict(provider_options or {}),
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
        template_origin: ProviderTemplateOrigin = "inline",
        auth_driver: str | None = None,
        provider_id: str | None = None,
        endpoint_id: str | None = None,
        model_id: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        provider_options: Mapping[str, Any] | None = None,
        enabled: bool = True,
        make_default: bool = False,
    ) -> ProviderConfig:
        return self._configure_template(
            template,
            template_origin=template_origin,
            operation="create",
            auth_driver=auth_driver,
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            model_id=model_id,
            base_url=base_url,
            api_path=api_path,
            api_key=api_key,
            api_key_env=api_key_env,
            timeout_seconds=timeout_seconds,
            retry_policy=retry_policy,
            provider_options=provider_options,
            enabled=enabled,
            make_default=make_default,
        )

    @atomic_config_update
    def update_from_template(
        self,
        template: ProviderTemplate,
        *,
        template_origin: ProviderTemplateOrigin = "inline",
        provider_id: str | None = None,
        endpoint_id: str | None = None,
        model_id: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        provider_options: Mapping[str, Any] | None = None,
        enabled: bool | None = None,
        make_default: bool = False,
    ) -> ProviderTemplateUpdateResult:
        return self._update_template_provider(
            template,
            template_origin=template_origin,
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            model_id=model_id,
            base_url=base_url,
            api_path=api_path,
            api_key=api_key,
            api_key_env=api_key_env,
            timeout_seconds=timeout_seconds,
            retry_policy=retry_policy,
            provider_options=provider_options,
            enabled=enabled,
            make_default=make_default,
        )

    @atomic_config_update
    def replace_from_template(
        self,
        template: ProviderTemplate,
        *,
        template_origin: ProviderTemplateOrigin = "inline",
        auth_driver: str | None = None,
        provider_id: str | None = None,
        endpoint_id: str | None = None,
        model_id: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        provider_options: Mapping[str, Any] | None = None,
        enabled: bool = True,
        make_default: bool = False,
    ) -> ProviderConfig:
        return self._configure_template(
            template,
            template_origin=template_origin,
            operation="replace",
            auth_driver=auth_driver,
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            model_id=model_id,
            base_url=base_url,
            api_path=api_path,
            api_key=api_key,
            api_key_env=api_key_env,
            timeout_seconds=timeout_seconds,
            retry_policy=retry_policy,
            provider_options=provider_options,
            enabled=enabled,
            make_default=make_default,
        )

    def _update_template_provider(
        self,
        template: ProviderTemplate,
        *,
        template_origin: ProviderTemplateOrigin,
        provider_id: str | None,
        endpoint_id: str | None,
        model_id: str | None,
        base_url: str | None,
        api_path: str | None,
        api_key: str | None,
        api_key_env: str | None,
        timeout_seconds: float | None,
        retry_policy: ProviderRetryPolicy | None,
        provider_options: Mapping[str, Any] | None,
        enabled: bool | None,
        make_default: bool,
    ) -> ProviderTemplateUpdateResult:
        """Three-way merge one configured provider against its latest template."""

        target_provider_id = self._validate_mutable_provider_id(
            provider_id if provider_id is not None else template.provider_id
        )
        raw = self.load_raw()
        configured = self._configured_providers(raw)
        current = configured.get(target_provider_id)
        if current is None:
            raise ConfigError(f"provider not found: {target_provider_id}")
        if current.provider_type != template.provider_type or current.protocol != template.protocol:
            raise ConfigError(
                f"provider type/protocol does not match template {template.template_id}: "
                f"{current.provider_type}/{current.protocol} != "
                f"{template.provider_type}/{template.protocol}"
            )

        previous = current.template
        if previous is None:
            raise ConfigError(
                "provider has no template provenance; replace it from a template before updating"
            )
        if previous.template_id != template.template_id:
            raise ConfigError(
                f"provider is bound to template {previous.template_id}, not {template.template_id}"
            )
        if previous.origin != template_origin:
            raise ConfigError(
                f"provider template source changed: {previous.origin} != "
                f"{template_origin}; replace the provider to accept a different source"
            )

        added: list[str] = []
        updated: list[str] = []
        removed: list[str] = []
        preserved: list[str] = []
        conflicts: list[str] = []
        updated_fields: list[str] = []
        preserved_fields: list[str] = []

        current_endpoints = {item.endpoint_id: item for item in current.endpoints}
        latest_endpoints = {item.endpoint_id: item for item in template.endpoints}
        next_endpoints = dict(current_endpoints)
        endpoint_ids: dict[str, str] = {}

        old_bindings = {item.template_endpoint_id: item for item in previous.endpoints}
        old_default_binding = old_bindings.get(previous.default_endpoint)
        old_configured_default = (
            old_default_binding.configured_endpoint_id if old_default_binding is not None else None
        )
        default_was_managed = current.default_endpoint == old_configured_default

        for template_endpoint_id, old_binding in old_bindings.items():
            configured_endpoint_id = old_binding.configured_endpoint_id
            existing = current_endpoints.get(configured_endpoint_id)
            latest = latest_endpoints.get(template_endpoint_id)
            if latest is None:
                if existing is None:
                    continue
                endpoint_was_managed = (
                    _template_endpoint_fingerprint(
                        existing,
                        template_endpoint_id=template_endpoint_id,
                    )
                    == old_binding.fingerprint
                )
                user_selected_endpoint = (
                    current.default_endpoint == configured_endpoint_id and not default_was_managed
                )
                if endpoint_was_managed and not user_selected_endpoint:
                    next_endpoints.pop(configured_endpoint_id, None)
                    removed.append(configured_endpoint_id)
                else:
                    preserved.append(configured_endpoint_id)
                continue

            endpoint_ids[template_endpoint_id] = configured_endpoint_id
            if existing is None:
                # Absence is a user edit just like a changed field. Keep the
                # binding so later template releases do not recreate it.
                preserved.append(configured_endpoint_id)
                continue
            endpoint_was_managed = (
                _template_endpoint_fingerprint(
                    existing,
                    template_endpoint_id=template_endpoint_id,
                )
                == old_binding.fingerprint
            )
            projected_latest = _configured_template_endpoint(
                latest,
                configured_endpoint_id=configured_endpoint_id,
            )
            if endpoint_was_managed:
                if existing != projected_latest:
                    next_endpoints[configured_endpoint_id] = projected_latest
                    updated.append(configured_endpoint_id)
            elif old_binding.fingerprint != _template_endpoint_fingerprint(latest):
                preserved.append(configured_endpoint_id)

        for template_endpoint in template.endpoints:
            template_endpoint_id = template_endpoint.endpoint_id
            if template_endpoint_id in endpoint_ids:
                continue
            configured_endpoint_id = template_endpoint_id
            if configured_endpoint_id in next_endpoints:
                conflicts.append(configured_endpoint_id)
                continue
            next_endpoints[configured_endpoint_id] = template_endpoint
            endpoint_ids[template_endpoint_id] = configured_endpoint_id
            added.append(configured_endpoint_id)

        next_default = current.default_endpoint
        latest_default_id = endpoint_ids.get(template.default_endpoint)
        if default_was_managed:
            if latest_default_id is not None and latest_default_id in next_endpoints:
                next_default = latest_default_id
                if next_default != current.default_endpoint:
                    updated_fields.append("default_endpoint")
            elif current.default_endpoint in current_endpoints:
                next_endpoints[current.default_endpoint] = current_endpoints[
                    current.default_endpoint
                ]
                if current.default_endpoint in removed:
                    removed.remove(current.default_endpoint)
                preserved.append(current.default_endpoint)
                preserved_fields.append("default_endpoint")
        elif previous.default_endpoint != template.default_endpoint:
            preserved_fields.append("default_endpoint")

        next_credential = current.credential_ref
        if previous.auth_driver != template.auth_driver:
            preserved_fields.append("auth_driver")
        if (
            current.auth_driver == previous.auth_driver == template.auth_driver == "static"
            and previous.credential_env != template.credential_env
        ):
            old_credential = CredentialRef.parse(
                f"env:{previous.credential_env}" if previous.credential_env else None
            )
            if current.credential_ref == old_credential:
                next_credential = CredentialRef.parse(
                    f"env:{template.credential_env}" if template.credential_env else None
                )
                updated_fields.append("credential")
            else:
                preserved_fields.append("credential")
        next_base_url = current.base_url
        if previous.base_url != template.base_url:
            if current.base_url == previous.base_url:
                next_base_url = template.base_url
                updated_fields.append("base_url")
            else:
                preserved_fields.append("base_url")
        next_api_path = current.api_path
        if previous.api_path != template.api_path:
            if current.api_path == previous.api_path:
                next_api_path = template.api_path
                updated_fields.append("api_path")
            else:
                preserved_fields.append("api_path")

        next_binding = _provider_template_binding(
            template,
            origin=template_origin,
            endpoint_ids=endpoint_ids,
        )

        if endpoint_id is not None or model_id is not None:
            default_binding = next_binding.endpoint_binding(template.default_endpoint)
            if default_binding is None:
                raise ConfigError(
                    "template default endpoint conflicts with an existing custom endpoint: "
                    f"{template.default_endpoint}"
                )
            source_endpoint_id = default_binding.configured_endpoint_id
            configured_default = next_endpoints.get(source_endpoint_id)
            if configured_default is None:
                configured_default = _configured_template_endpoint(
                    template.endpoint(),
                    configured_endpoint_id=source_endpoint_id,
                )
                next_endpoints[source_endpoint_id] = configured_default
                added.append(source_endpoint_id)
            target_endpoint_id = (
                endpoint_id.strip() if endpoint_id is not None else source_endpoint_id
            )
            if not target_endpoint_id:
                raise ConfigError("endpoint id is required")
            if target_endpoint_id != source_endpoint_id and target_endpoint_id in next_endpoints:
                raise ConfigError(
                    "default endpoint override conflicts with an existing endpoint: "
                    f"{target_endpoint_id}"
                )
            next_endpoints.pop(source_endpoint_id, None)
            next_endpoints[target_endpoint_id] = ProviderEndpointConfig.model_validate(
                configured_default.model_copy(
                    update={
                        "endpoint_id": target_endpoint_id,
                        "model_id": model_id
                        if model_id is not None
                        else configured_default.model_id,
                    }
                ).model_dump()
            )
            if target_endpoint_id != source_endpoint_id:
                for reported_endpoint_ids in (added, updated, preserved):
                    if source_endpoint_id in reported_endpoint_ids:
                        reported_endpoint_ids.remove(source_endpoint_id)
                        reported_endpoint_ids.append(target_endpoint_id)
            endpoint_ids[template.default_endpoint] = target_endpoint_id
            next_binding = _provider_template_binding(
                template,
                origin=template_origin,
                endpoint_ids=endpoint_ids,
            )
            next_default = target_endpoint_id

        next_credential = self._updated_credential(
            next_credential,
            api_key=api_key,
            api_key_env=api_key_env,
        )
        try:
            provider = ProviderConfig(
                provider_id=target_provider_id,
                provider_type=current.provider_type,
                protocol=current.protocol,
                auth_driver=current.auth_driver,
                credential_ref=next_credential,
                default_endpoint=next_default,
                endpoints=tuple(next_endpoints[key] for key in sorted(next_endpoints)),
                base_url=base_url if base_url is not None else next_base_url,
                api_path=api_path if api_path is not None else next_api_path,
                enabled=enabled if enabled is not None else current.enabled,
                timeout_seconds=(
                    timeout_seconds if timeout_seconds is not None else current.timeout_seconds
                ),
                retry_policy=retry_policy or current.retry_policy,
                extra=(dict(provider_options) if provider_options is not None else current.extra),
                template=next_binding,
            )
        except ValidationError as exc:
            raise ConfigError(f"invalid provider template update: {exc}") from exc
        self._store_provider(
            raw,
            configured=configured,
            provider=provider,
            make_default=make_default,
        )
        return ProviderTemplateUpdateResult(
            provider=provider,
            template_id=template.template_id,
            from_revision=previous.revision,
            to_revision=template.revision,
            changed=provider != current,
            added_endpoints=tuple(sorted(set(added))),
            updated_endpoints=tuple(sorted(set(updated))),
            removed_endpoints=tuple(sorted(set(removed))),
            preserved_endpoints=tuple(sorted(set(preserved))),
            conflicted_endpoints=tuple(sorted(set(conflicts))),
            updated_fields=tuple(sorted(set(updated_fields))),
            preserved_fields=tuple(sorted(set(preserved_fields))),
        )

    def _configure_template(
        self,
        template: ProviderTemplate,
        *,
        template_origin: ProviderTemplateOrigin,
        operation: Literal["create", "replace"],
        auth_driver: str | None = None,
        provider_id: str | None = None,
        endpoint_id: str | None = None,
        model_id: str | None = None,
        base_url: str | None = None,
        api_path: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
        provider_options: Mapping[str, Any] | None = None,
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
        if operation == "replace" and current is None:
            raise ConfigError(f"provider not found: {target_provider_id}")

        selected_driver = auth_driver if auth_driver is not None else template.auth_driver
        credential_ref = self._initial_credential(selected_driver)
        credential_ref = self._updated_credential(
            credential_ref, api_key=api_key, api_key_env=api_key_env
        )
        if credential_ref.type == "none" and template.credential_env is not None:
            credential_ref = CredentialRef.parse(f"env:{template.credential_env}")

        try:
            endpoints: dict[str, ProviderEndpointConfig] = {}
            endpoint_ids: dict[str, str] = {}
            for template_endpoint in template.endpoints:
                is_default = template_endpoint.endpoint_id == template.default_endpoint
                configured_endpoint_id = (
                    target_endpoint_id if is_default else template_endpoint.endpoint_id
                )
                configured_model_id = (
                    model_id if is_default and model_id is not None else template_endpoint.model_id
                )
                endpoint_ids[template_endpoint.endpoint_id] = configured_endpoint_id
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
                auth_driver=selected_driver,
                credential_ref=credential_ref,
                default_endpoint=target_endpoint_id,
                endpoints=tuple(endpoints[key] for key in sorted(endpoints)),
                base_url=base_url if base_url is not None else template.base_url,
                api_path=api_path if api_path is not None else template.api_path,
                enabled=(enabled if enabled is not None else True),
                timeout_seconds=(timeout_seconds if timeout_seconds is not None else 60.0),
                retry_policy=retry_policy or ProviderRetryPolicy(),
                extra=(dict(provider_options) if provider_options is not None else {}),
                template=_provider_template_binding(
                    template,
                    origin=template_origin,
                    endpoint_ids=endpoint_ids,
                ),
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
    def set_default_route(
        self,
        provider_id: str,
        endpoint_id: str,
    ) -> ProviderEndpointConfig:
        """Atomically select the global provider/endpoint route."""

        raw = self.load_raw()
        configured = self._configured_providers(raw)
        provider = configured.get(provider_id)
        if provider is None:
            raise ConfigError(f"provider not found: {provider_id}")
        if not provider.enabled:
            raise ConfigError(f"provider is disabled: {provider_id}")
        endpoint = provider.endpoint(endpoint_id)
        if endpoint is None:
            raise ConfigError(f"endpoint not found: {provider_id}/{endpoint_id}")
        configured[provider_id] = provider.model_copy(update={"default_endpoint": endpoint_id})
        self._save_configured_providers(
            raw,
            configured=configured,
            default_provider_id=provider_id,
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
    def _initial_credential(auth_driver: str) -> CredentialRef:
        if auth_driver == "static":
            return CredentialRef()
        return CredentialRef(type="managed", name=new_id("cred"))

    @staticmethod
    def _updated_credential(
        current: CredentialRef,
        *,
        api_key: str | None,
        api_key_env: str | None,
    ) -> CredentialRef:
        if current.type == "managed" and (api_key is not None or api_key_env is not None):
            raise ConfigError("API Key overrides cannot be combined with managed authentication")
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


def _template_endpoint_fingerprint(
    endpoint: ProviderEndpointConfig,
    *,
    template_endpoint_id: str | None = None,
) -> str:
    """Fingerprint endpoint business fields under its stable template identity."""

    normalized = (
        endpoint
        if template_endpoint_id is None
        else endpoint.model_copy(update={"endpoint_id": template_endpoint_id})
    )
    payload = json.dumps(
        normalized.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _configured_template_endpoint(
    endpoint: ProviderEndpointConfig,
    *,
    configured_endpoint_id: str,
) -> ProviderEndpointConfig:
    return ProviderEndpointConfig.model_validate(
        endpoint.model_copy(update={"endpoint_id": configured_endpoint_id}).model_dump()
    )


def _provider_template_binding(
    template: ProviderTemplate,
    *,
    origin: ProviderTemplateOrigin,
    endpoint_ids: Mapping[str, str],
) -> ProviderTemplateBinding:
    return ProviderTemplateBinding(
        template_id=template.template_id,
        origin=origin,
        revision=template.revision,
        auth_driver=template.auth_driver,
        credential_env=template.credential_env,
        base_url=template.base_url,
        api_path=template.api_path,
        default_endpoint=template.default_endpoint,
        endpoints=tuple(
            ProviderTemplateEndpointBinding(
                template_endpoint_id=endpoint.endpoint_id,
                configured_endpoint_id=endpoint_ids[endpoint.endpoint_id],
                fingerprint=_template_endpoint_fingerprint(endpoint),
            )
            for endpoint in template.endpoints
            if endpoint.endpoint_id in endpoint_ids
        ),
    )
