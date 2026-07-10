from __future__ import annotations

from typing import Any

from ouro.config.provider_models import (
    ProviderConfig,
    ProviderRuntimeOptions,
    default_api_path,
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
        provider=provider.provider_type,
        provider_id=provider.provider_id,
        endpoint_id=endpoint.endpoint_id,
        model=endpoint.model_id,
        base_url=endpoint.base_url or provider.base_url,
        api_path=endpoint.api_path or provider.api_path or default_api_path(provider.provider_type),
        credential_ref=None if provider.provider_type == "faux" else provider.credential_ref,
        timeout=provider.timeout_seconds,
        temperature=endpoint.temperature,
        context_window=endpoint.context_window,
        max_output_tokens=endpoint.max_output_tokens,
        supports_tools=endpoint.supports_tools,
        supports_vision=endpoint.supports_vision,
        supports_streaming=endpoint.supports_streaming,
        supports_structured_output=endpoint.supports_structured_output,
        supports_reasoning=endpoint.supports_reasoning,
        supported_thinking_levels=endpoint.supported_thinking_levels,
        thinking_level=endpoint.default_thinking_level,
        extra={**provider.extra, **endpoint.request_options},
    )


def merge_runtime_options(
    base: ProviderRuntimeOptions,
    override: ProviderRuntimeOptions,
) -> ProviderRuntimeOptions:
    """Apply explicit runtime options over the selected configured endpoint."""

    supports_reasoning = (
        override.supports_reasoning
        if override.supports_reasoning is not None
        else base.supports_reasoning
    )
    supported_thinking_levels = (
        override.supported_thinking_levels
        if override.supported_thinking_levels is not None
        else (() if override.supports_reasoning is False else base.supported_thinking_levels)
    )
    thinking_level = (
        override.thinking_level
        if override.thinking_level is not None
        else (None if override.supports_reasoning is False else base.thinking_level)
    )
    return ProviderRuntimeOptions(
        provider=override.provider if override.provider is not None else base.provider,
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
        supports_vision=(
            override.supports_vision
            if override.supports_vision is not None
            else base.supports_vision
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
        supports_reasoning=supports_reasoning,
        supported_thinking_levels=supported_thinking_levels,
        thinking_level=thinking_level,
        extra=_merge_extra(base.extra, override.extra),
    )


def _merge_extra(
    base: dict[str, Any] | None,
    override: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if base is None and override is None:
        return None
    merged = dict(base or {})
    merged.update(override or {})
    return merged
