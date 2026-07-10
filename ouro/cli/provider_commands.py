from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from ouro.cli.errors import error_exit_code
from ouro.cli.run import RuntimeProviderOptions, build_default_runtime
from ouro.config import (
    ProviderConfigManager,
    ProviderKind,
    endpoint_config_to_public_dict,
    provider_config_to_public_dict,
)
from ouro.core.errors import ConfigError, OuroError
from ouro.core.provider import ThinkingLevel


async def check_provider_connection(
    *,
    home: str | Path | None = None,
    provider_options: RuntimeProviderOptions | None = None,
    provider_id: str | None = None,
    endpoint_id: str | None = None,
    json_mode: bool = False,
) -> int:
    try:
        effective_options = _provider_options_with_target(
            provider_options,
            provider_id=provider_id,
            endpoint_id=endpoint_id,
        )
        runtime = build_default_runtime(home, provider_options=effective_options)
        result = await runtime.adapter.provider.check_connection(runtime.adapter.model)
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok" if result.ok else "failed",
                    **result.model_dump(mode="json"),
                },
                ensure_ascii=False,
            )
        )
    elif result.ok:
        target = _checked_target_label(
            result.provider_id,
            runtime.adapter.model.endpoint_id,
            result.model_id,
        )
        print(f"Provider check OK: {target}")
    else:
        target = _checked_target_label(
            result.provider_id,
            runtime.adapter.model.endpoint_id,
            result.model_id,
        )
        print(
            f"Provider check failed: {target} - {result.message}",
            file=sys.stderr,
        )
    return 0 if result.ok else 3


def list_provider_configs(
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        manager = ProviderConfigManager(home)
        snapshot = manager.snapshot()
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    providers = [
        provider_config_to_public_dict(
            provider,
            is_default=provider.provider_id == snapshot.default_provider_id,
        )
        for provider in snapshot.providers
    ]
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "default": snapshot.default_provider_id,
                    "config_path": str(snapshot.config_path),
                    "providers": providers,
                },
                ensure_ascii=False,
            )
        )
        return 0

    lines = [f"Providers (default: {snapshot.default_provider_id}):"]
    for provider in providers:
        marker = "*" if provider["default"] else "-"
        enabled = "enabled" if provider["enabled"] else "disabled"
        endpoint = provider["default_endpoint"] or "-"
        lines.append(
            f"{marker} {provider['provider_id']} {provider['kind']} "
            f"endpoint={endpoint} endpoints={provider['endpoint_count']} {enabled}"
        )
    print("\n".join(lines))
    return 0


def show_provider_config(
    provider_id: str | None = None,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        manager = ProviderConfigManager(home)
        snapshot = manager.snapshot()
        target_id = provider_id or snapshot.default_provider_id
        provider = manager.get_provider(target_id)
        if provider is None:
            raise ConfigError(f"provider not found: {target_id}")
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = provider_config_to_public_dict(
        provider,
        is_default=provider.provider_id == snapshot.default_provider_id,
    )
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "config_path": str(snapshot.config_path),
                    "provider": public,
                },
                ensure_ascii=False,
            )
        )
        return 0

    print(
        "\n".join(
            [
                f"Provider: {public['provider_id']}",
                f"Kind: {public['kind']}",
                f"Credential: {public['credential']}",
                f"Timeout: {public['timeout']}",
                f"Enabled: {'yes' if public['enabled'] else 'no'}",
                f"Default provider: {'yes' if public['default'] else 'no'}",
                f"Default endpoint: {public['default_endpoint'] or '-'}",
                f"Endpoints: {public['endpoint_count']}",
                f"Config: {snapshot.config_path}",
            ]
        )
    )
    return 0


def set_provider_config(
    provider_id: str,
    *,
    home: str | Path | None = None,
    provider_type: ProviderKind | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    timeout: float | None = None,
    enabled: bool | None = None,
    make_default: bool = False,
    json_mode: bool = False,
) -> int:
    try:
        manager = ProviderConfigManager(home)
        provider = manager.upsert_provider(
            provider_id,
            provider_type=provider_type,
            default_model=model,
            base_url=base_url,
            api_key=api_key,
            api_key_env=api_key_env,
            timeout_seconds=timeout,
            enabled=enabled,
            make_default=make_default,
        )
        snapshot = manager.snapshot()
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = provider_config_to_public_dict(
        provider,
        is_default=provider.provider_id == snapshot.default_provider_id,
    )
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "config_path": str(snapshot.config_path),
                    "provider": public,
                },
                ensure_ascii=False,
            )
        )
        return 0
    suffix = " and set as default" if public["default"] else ""
    print(f"Provider saved: {provider.provider_id}{suffix}")
    return 0


def use_provider_config(
    provider_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        manager = ProviderConfigManager(home)
        provider = manager.set_default_provider(provider_id)
        snapshot = manager.snapshot()
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = provider_config_to_public_dict(provider, is_default=True)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "default": provider.provider_id,
                    "config_path": str(snapshot.config_path),
                    "provider": public,
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(f"Default provider: {provider.provider_id}")
    return 0


def remove_provider_config(
    provider_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        manager = ProviderConfigManager(home)
        manager.remove_provider(provider_id)
        snapshot = manager.snapshot()
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "removed": provider_id,
                    "default": snapshot.default_provider_id,
                    "config_path": str(snapshot.config_path),
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(f"Provider removed: {provider_id}")
    return 0


def list_provider_endpoints(
    provider_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        manager = ProviderConfigManager(home)
        provider = manager.get_provider(provider_id)
        if provider is None:
            raise ConfigError(f"provider not found: {provider_id}")
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    endpoints = [
        {
            **endpoint_config_to_public_dict(endpoint),
            "default": endpoint.endpoint_id == provider.default_endpoint,
        }
        for endpoint in provider.endpoints
    ]
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "provider_id": provider.provider_id,
                    "default_endpoint": provider.default_endpoint,
                    "endpoints": endpoints,
                },
                ensure_ascii=False,
            )
        )
        return 0

    lines = [f"Endpoints for {provider.provider_id} (default: {provider.default_endpoint or '-'}):"]
    for endpoint in endpoints:
        marker = "*" if endpoint["default"] else "-"
        lines.append(
            f"{marker} {endpoint['endpoint_id']} model={endpoint['model_id']} "
            f"context={endpoint['context_window']}"
        )
    print("\n".join(lines))
    return 0


def show_provider_endpoint(
    provider_id: str,
    endpoint_id: str | None = None,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        manager = ProviderConfigManager(home)
        provider = manager.get_provider(provider_id)
        if provider is None:
            raise ConfigError(f"provider not found: {provider_id}")
        endpoint = provider.endpoint(endpoint_id)
        if endpoint is None:
            target = endpoint_id or provider.default_endpoint or "<default>"
            raise ConfigError(f"endpoint not found: {provider_id}/{target}")
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = {
        **endpoint_config_to_public_dict(endpoint),
        "default": endpoint.endpoint_id == provider.default_endpoint,
    }
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "provider_id": provider.provider_id,
                    "endpoint": public,
                },
                ensure_ascii=False,
            )
        )
        return 0
    lines = [f"Endpoint: {provider.provider_id}/{endpoint.endpoint_id}"]
    for key, value in public.items():
        if key not in {"endpoint_id", "request_options", "metadata"}:
            lines.append(f"{key}: {value}")
    print("\n".join(lines))
    return 0


def set_provider_endpoint(
    provider_id: str,
    endpoint_id: str,
    *,
    home: str | Path | None = None,
    model: str | None = None,
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
    json_mode: bool = False,
) -> int:
    try:
        manager = ProviderConfigManager(home)
        endpoint = manager.upsert_endpoint(
            provider_id,
            endpoint_id,
            model_id=model,
            base_url=base_url,
            api_path=api_path,
            temperature=temperature,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            supports_tools=supports_tools,
            supports_vision=supports_vision,
            supports_streaming=supports_streaming,
            supports_structured_output=supports_structured_output,
            supports_reasoning=supports_reasoning,
            supported_thinking_levels=supported_thinking_levels,
            default_thinking_level=default_thinking_level,
            request_options=request_options,
            make_default=make_default,
        )
        provider = manager.get_provider(provider_id)
        assert provider is not None
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = {
        **endpoint_config_to_public_dict(endpoint),
        "default": endpoint.endpoint_id == provider.default_endpoint,
    }
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "provider_id": provider_id,
                    "endpoint": public,
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(f"Endpoint saved: {provider_id}/{endpoint_id}")
    return 0


def use_provider_endpoint(
    provider_id: str,
    endpoint_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        endpoint = ProviderConfigManager(home).set_default_endpoint(
            provider_id,
            endpoint_id,
        )
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "provider_id": provider_id,
                    "default_endpoint": endpoint.endpoint_id,
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(f"Default endpoint: {provider_id}/{endpoint.endpoint_id}")
    return 0


def remove_provider_endpoint(
    provider_id: str,
    endpoint_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        manager = ProviderConfigManager(home)
        manager.remove_endpoint(provider_id, endpoint_id)
        provider = manager.get_provider(provider_id)
        snapshot = manager.snapshot()
        assert provider is not None
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "provider_id": provider_id,
                    "removed": endpoint_id,
                    "default_endpoint": provider.default_endpoint,
                    "default_provider": snapshot.default_provider_id,
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(
        f"Endpoint removed: {provider_id}/{endpoint_id} "
        f"(default provider: {snapshot.default_provider_id})"
    )
    return 0


def _provider_options_with_target(
    options: RuntimeProviderOptions | None,
    *,
    provider_id: str | None,
    endpoint_id: str | None,
) -> RuntimeProviderOptions:
    base = options or RuntimeProviderOptions()
    return replace(
        base,
        provider_id=base.provider_id or provider_id,
        endpoint_id=base.endpoint_id or endpoint_id,
    )


def _checked_target_label(
    provider_id: str,
    endpoint_id: str | None,
    model_id: str | None,
) -> str:
    parts = [provider_id]
    if endpoint_id:
        parts.append(endpoint_id)
    if model_id and model_id != endpoint_id:
        parts.append(model_id)
    return "/".join(parts)


def _print_provider_error(exc: OuroError, *, json_mode: bool) -> None:
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"Error: {exc}", file=sys.stderr)
