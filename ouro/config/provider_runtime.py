from __future__ import annotations

from typing import Any

from ouro.config.credentials import CredentialRef
from ouro.config.provider_models import (
    ProviderConfig,
    ProviderRuntimeOptions,
    default_api_path,
    resolve_provider_connection,
    validate_credential_binding,
)
from ouro.core.errors import ConfigError


def runtime_options_from_config(
    provider: ProviderConfig,
    *,
    endpoint_id: str | None,
) -> ProviderRuntimeOptions:
    """Resolve one configured endpoint into provider-construction options."""

    endpoint = provider.endpoint(endpoint_id)
    if endpoint is None:
        target = endpoint_id or provider.default_endpoint or "<default>"
        raise ConfigError(f"endpoint not found: {provider.provider_id}/{target}")
    return ProviderRuntimeOptions(
        protocol=provider.protocol,
        auth_driver=provider.auth_driver,
        service_policy=resolve_provider_connection(
            provider.provider_type, provider.protocol, provider.auth_driver
        ).service_policy,
        provider_id=provider.provider_id,
        endpoint_id=endpoint.endpoint_id,
        model=endpoint.model_id,
        base_url=endpoint.base_url or provider.base_url,
        api_path=endpoint.api_path or provider.api_path or default_api_path(provider.protocol),
        credential_ref=None if provider.protocol == "faux" else provider.credential_ref,
        timeout=provider.timeout_seconds,
        temperature=endpoint.temperature,
        context_window=endpoint.context_window,
        max_output_tokens=endpoint.max_output_tokens,
        supports_tools=endpoint.supports_tools,
        supports_image_input=endpoint.supports_image_input,
        supports_audio_input=endpoint.supports_audio_input,
        supports_video_input=endpoint.supports_video_input,
        supports_streaming=endpoint.supports_streaming,
        supports_structured_output=endpoint.supports_structured_output,
        reasoning=endpoint.reasoning,
        retry_policy=provider.retry_policy,
        headers=dict(provider.headers),
        extra={**provider.extra, **endpoint.request_options},
    )


def merge_runtime_options(
    base: ProviderRuntimeOptions,
    override: ProviderRuntimeOptions,
) -> ProviderRuntimeOptions:
    """Apply explicit runtime options over the selected configured endpoint."""

    if (
        base.auth_driver is not None
        and override.auth_driver is not None
        and override.auth_driver != base.auth_driver
    ):
        raise ConfigError("runtime options cannot change a configured authentication driver")
    if (
        base.auth_driver not in (None, "static")
        and override.credential_ref is not None
        and override.credential_ref != base.credential_ref
    ):
        raise ConfigError("runtime options cannot change a managed credential binding")
    if override.service_policy is not None and override.service_policy != base.service_policy:
        raise ConfigError("runtime options cannot override the product service policy")
    merged = ProviderRuntimeOptions(
        protocol=override.protocol if override.protocol is not None else base.protocol,
        auth_driver=override.auth_driver or base.auth_driver or "static",
        service_policy=base.service_policy,
        provider_id=override.provider_id if override.provider_id is not None else base.provider_id,
        endpoint_id=override.endpoint_id if override.endpoint_id is not None else base.endpoint_id,
        model=override.model if override.model is not None else base.model,
        base_url=override.base_url if override.base_url is not None else base.base_url,
        api_path=override.api_path if override.api_path is not None else base.api_path,
        api_key=override.api_key if override.api_key is not None else base.api_key,
        api_key_env=override.api_key_env if override.api_key_env is not None else base.api_key_env,
        credential_ref=(
            override.credential_ref if override.credential_ref is not None else base.credential_ref
        ),
        timeout=override.timeout if override.timeout is not None else base.timeout,
        temperature=override.temperature if override.temperature is not None else base.temperature,
        context_window=(
            override.context_window if override.context_window is not None else base.context_window
        ),
        max_output_tokens=(
            override.max_output_tokens
            if override.max_output_tokens is not None
            else base.max_output_tokens
        ),
        supports_tools=(
            override.supports_tools if override.supports_tools is not None else base.supports_tools
        ),
        supports_image_input=(
            override.supports_image_input
            if override.supports_image_input is not None
            else base.supports_image_input
        ),
        supports_audio_input=(
            override.supports_audio_input
            if override.supports_audio_input is not None
            else base.supports_audio_input
        ),
        supports_video_input=(
            override.supports_video_input
            if override.supports_video_input is not None
            else base.supports_video_input
        ),
        supports_streaming=(
            override.supports_streaming
            if override.supports_streaming is not None
            else base.supports_streaming
        ),
        supports_structured_output=(
            override.supports_structured_output
            if override.supports_structured_output is not None
            else base.supports_structured_output
        ),
        reasoning=override.reasoning if override.reasoning is not None else base.reasoning,
        reasoning_request=(
            override.reasoning_request
            if override.reasoning_request is not None
            else base.reasoning_request
        ),
        retry_policy=(
            override.retry_policy if override.retry_policy is not None else base.retry_policy
        ),
        headers=override.headers if override.headers is not None else base.headers,
        extra=_merge_extra(base.extra, override.extra),
    )
    validate_runtime_auth(merged)
    return merged


def validate_runtime_auth(options: ProviderRuntimeOptions) -> None:
    """Validate final authentication options, including direct factory callers."""
    driver = options.auth_driver if options.auth_driver is not None else "static"
    try:
        validate_credential_binding(driver, options.credential_ref or CredentialRef())
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    if driver != "static":
        if options.api_key is not None or options.api_key_env is not None:
            raise ConfigError("API Key overrides cannot be combined with managed authentication")
        if not options.service_policy:
            raise ConfigError("managed authentication requires a product service policy")


def _merge_extra(
    base: dict[str, Any] | None,
    override: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if base is None and override is None:
        return None
    merged = dict(base or {})
    merged.update(override or {})
    return merged
