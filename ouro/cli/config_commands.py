from __future__ import annotations

import json
import sys
from pathlib import Path

from ouro.cli.errors import error_exit_code
from ouro.cli.run import RuntimeProviderOptions, build_default_runtime
from ouro.config import (
    PromptConfigManager,
    ProviderConfigManager,
    ProviderKind,
    prompt_config_to_public_dict,
    provider_config_to_public_dict,
)
from ouro.core.errors import ConfigError, OuroError


async def check_provider_connection(
    *,
    home: str | Path | None = None,
    provider_options: RuntimeProviderOptions | None = None,
    provider_id: str | None = None,
    json_mode: bool = False,
) -> int:
    try:
        effective_options = _provider_options_with_id(provider_options, provider_id)
        runtime = build_default_runtime(home, provider_options=effective_options)
        result = await runtime.adapter.provider.check_connection(runtime.adapter.model)
    except OuroError as exc:
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
        print(f"Provider check OK: {result.provider_id}/{result.model_id or '-'}")
    else:
        print(
            f"Provider check failed: {result.provider_id}/{result.model_id or '-'} - "
            f"{result.message}",
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
        _print_config_error(exc, json_mode=json_mode)
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

    if not providers:
        print("No providers configured.")
        return 0
    lines = [f"Providers (default: {snapshot.default_provider_id}):"]
    for provider in providers:
        marker = "*" if provider["default"] else "-"
        enabled = "enabled" if provider["enabled"] else "disabled"
        model = provider["model"] or "-"
        lines.append(
            f"{marker} {provider['provider_id']} {provider['kind']} model={model} {enabled}"
        )
    print("\n".join(lines))
    return 0


def show_system_prompt_config(
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        manager = PromptConfigManager(home)
        snapshot = manager.snapshot()
    except OuroError as exc:
        _print_config_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = prompt_config_to_public_dict(snapshot)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "config_path": str(snapshot.config_path),
                    **public,
                },
                ensure_ascii=False,
            )
        )
        return 0

    configured = "yes" if public["configured"] else "no"
    print(
        "\n".join(
            [
                f"System prompt configured: {configured}",
                f"Config: {snapshot.config_path}",
                "System prompt:",
                public["effective_system_prompt"],
            ]
        )
    )
    return 0


def set_system_prompt_config(
    prompt: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        manager = PromptConfigManager(home)
        snapshot = manager.set_system_prompt(prompt)
    except OuroError as exc:
        _print_config_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "config_path": str(snapshot.config_path),
                    **prompt_config_to_public_dict(snapshot),
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(f"System prompt saved: {snapshot.config_path}")
    return 0


def clear_system_prompt_config(
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        manager = PromptConfigManager(home)
        snapshot = manager.clear_system_prompt()
    except OuroError as exc:
        _print_config_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "config_path": str(snapshot.config_path),
                    **prompt_config_to_public_dict(snapshot),
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(f"System prompt cleared: {snapshot.config_path}")
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
        _print_config_error(exc, json_mode=json_mode)
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
                f"Model: {public['model'] or '-'}",
                f"Base URL: {public['base_url'] or '-'}",
                f"Credential: {public['credential']}",
                f"Timeout: {public['timeout']}",
                f"Enabled: {'yes' if public['enabled'] else 'no'}",
                f"Default: {'yes' if public['default'] else 'no'}",
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
        _print_config_error(exc, json_mode=json_mode)
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
        _print_config_error(exc, json_mode=json_mode)
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
        _print_config_error(exc, json_mode=json_mode)
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


def _provider_options_with_id(
    options: RuntimeProviderOptions | None,
    provider_id: str | None,
) -> RuntimeProviderOptions:
    base = options or RuntimeProviderOptions()
    if provider_id is None or base.provider_id is not None:
        return base
    return RuntimeProviderOptions(
        provider=base.provider,
        provider_id=provider_id,
        model=base.model,
        base_url=base.base_url,
        api_key=base.api_key,
        api_key_env=base.api_key_env,
        credential_ref=base.credential_ref,
        timeout=base.timeout,
    )


def _print_config_error(exc: OuroError, *, json_mode: bool) -> None:
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
