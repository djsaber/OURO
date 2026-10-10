from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from ouro.application.configuration import ConfigurationFacade
from ouro.application.configuration_models import (
    ProviderEndpointView,
    ProviderTemplateUpdateView,
    ProviderTemplateView,
    ProviderView,
)
from ouro.cli.errors import error_exit_code
from ouro.cli.run import RuntimeProviderOptions, build_default_runtime
from ouro.config import EndpointReasoningConfig, ProviderProtocol, ProviderType
from ouro.core.base import new_id
from ouro.core.errors import OuroError
from ouro.core.request_context import ProviderRequestContext


async def check_provider_connection(
    *,
    home: str | Path | None = None,
    provider_options: RuntimeProviderOptions | None = None,
    json_mode: bool = False,
) -> int:
    try:
        options = provider_options or RuntimeProviderOptions()
        ConfigurationFacade(home).provider(options.provider_id)
        runtime = build_default_runtime(
            home,
            provider_options=options,
        )
        async with runtime:
            result = await runtime.adapter.provider.check_connection(
                runtime.adapter.model,
                request_context=ProviderRequestContext(
                    {
                        "session.id": new_id("check"),
                        "request.purpose": "connection_check",
                    }
                ),
            )
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
        catalog = ConfigurationFacade(home).provider_catalog()
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    providers = [_provider_to_public(provider) for provider in catalog.providers]
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "default": catalog.default_provider_id,
                    "config_path": catalog.config_path,
                    "providers": providers,
                },
                ensure_ascii=False,
            )
        )
        return 0

    lines = [f"Providers (default: {catalog.default_provider_id or '-'}):"]
    for provider in providers:
        marker = "*" if provider["default"] else "-"
        enabled = "enabled" if provider["enabled"] else "disabled"
        endpoint = provider["default_endpoint"] or "-"
        lines.append(
            f"{marker} {provider['display_name']} ({provider['provider_id']}) "
            f"type={provider['provider_type']} "
            f"protocol={provider['protocol']} "
            f"endpoint={endpoint} endpoints={provider['endpoint_count']} {enabled}"
        )
    print("\n".join(lines))
    return 0


def list_provider_templates(
    template_id: str | None = None,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        facade = ConfigurationFacade(home)
        catalog = facade.provider_template_catalog()
        templates = (
            [facade.provider_template(template_id)]
            if template_id is not None
            else catalog.templates
        )
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = [_template_to_public(template) for template in templates]
    if json_mode:
        print(
            json.dumps(
                {"status": "ok", "templates": public, "notices": catalog.notices},
                ensure_ascii=False,
            )
        )
        return 0

    lines = ["Provider templates:"]
    for template in public:
        models = ",".join(endpoint["model_id"] for endpoint in template["endpoints"])
        deprecated = " deprecated" if template.get("deprecated") else ""
        credential = (
            f"env:{template['credential_env']}"
            if template.get("credential_env") is not None
            else "none"
            if template["auth_driver"] == "static"
            else "managed"
        )
        lines.append(
            f"- {template['template_id']} ({template['display_name']}){deprecated} "
            f"type={template['provider_type']} protocol={template['protocol']} "
            f"default={template['default_endpoint']} "
            f"endpoints={len(template['endpoints'])} models={models} "
            f"credential={credential}"
        )
        if template.get("notice"):
            lines.append(f"  {template['notice']}")
    lines.extend(f"! {notice}" for notice in catalog.notices)
    print("\n".join(lines))
    return 0


def restore_provider_template(
    template_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        facade = ConfigurationFacade(home)
        template = facade.restore_provider_template(template_id)
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    if json_mode:
        print(
            json.dumps(
                {"status": "ok", "template": _template_to_public(template)},
                ensure_ascii=False,
            )
        )
        return 0

    print(
        f"Restored provider template {template.template_id} "
        f"({template.display_name}) to the inline catalog version."
    )
    return 0


def show_provider_config(
    provider_id: str | None = None,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        facade = ConfigurationFacade(home)
        provider = facade.provider(provider_id)
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = _provider_to_public(provider)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "config_path": str(facade.config_path),
                    "provider": public,
                },
                ensure_ascii=False,
            )
        )
        return 0

    print(
        "\n".join(
            [
                f"Provider: {public['display_name']} ({public['provider_id']})",
                f"Type: {public['provider_type']}",
                f"Protocol: {public['protocol']}",
                f"Credential: {public['credential']}",
                f"Timeout: {public['timeout']}",
                "Headers: " + json.dumps(public["headers"], ensure_ascii=False),
                "Request defaults: " + json.dumps(public["provider_options"], ensure_ascii=False),
                f"Enabled: {'yes' if public['enabled'] else 'no'}",
                f"Default provider: {'yes' if public['default'] else 'no'}",
                f"Default endpoint: {public['default_endpoint'] or '-'}",
                f"Endpoints: {public['endpoint_count']}",
                f"Config: {facade.config_path}",
                *([public["notice"]] if public.get("notice") else []),
            ]
        )
    )
    return 0


def mutate_provider_config(
    operation: Literal["add", "update", "replace"],
    provider_id: str | None,
    *,
    home: str | Path | None = None,
    display_name: str | None = None,
    provider_type: ProviderType | None = None,
    protocol: ProviderProtocol | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_path: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    timeout: float | None = None,
    headers: Mapping[str, Any] | None = None,
    provider_options: Mapping[str, Any] | None = None,
    retry_max_attempts: int | None = None,
    retry_initial_backoff_seconds: float | None = None,
    retry_max_backoff_seconds: float | None = None,
    retry_backoff_multiplier: float | None = None,
    enabled: bool | None = None,
    make_default: bool = False,
    json_mode: bool = False,
) -> int:
    try:
        facade = ConfigurationFacade(home)
        if operation == "update":
            if provider_id is None:
                raise AssertionError("provider id is required for update")
            catalog = facade.update_provider(
                provider_id,
                display_name=display_name,
                model=model,
                base_url=base_url,
                api_path=api_path,
                api_key=api_key,
                api_key_env=api_key_env,
                timeout=timeout,
                headers=headers,
                provider_options=provider_options,
                retry_max_attempts=retry_max_attempts,
                retry_initial_backoff_seconds=retry_initial_backoff_seconds,
                retry_max_backoff_seconds=retry_max_backoff_seconds,
                retry_backoff_multiplier=retry_backoff_multiplier,
                enabled=enabled,
                make_default=make_default,
            )
        else:
            if provider_type is None:
                raise AssertionError(f"provider type is required for {operation}")
            configured_enabled = True if enabled is None else enabled
            if operation == "add":
                if display_name is None:
                    raise AssertionError("provider display name is required for add")
                catalog = facade.add_provider(
                    display_name=display_name,
                    provider_type=provider_type,
                    protocol=protocol,
                    model=model,
                    base_url=base_url,
                    api_path=api_path,
                    api_key=api_key,
                    api_key_env=api_key_env,
                    timeout=timeout,
                    headers=headers,
                    provider_options=provider_options,
                    retry_max_attempts=retry_max_attempts,
                    retry_initial_backoff_seconds=retry_initial_backoff_seconds,
                    retry_max_backoff_seconds=retry_max_backoff_seconds,
                    retry_backoff_multiplier=retry_backoff_multiplier,
                    enabled=configured_enabled,
                    make_default=make_default,
                )
            else:
                if provider_id is None:
                    raise AssertionError("provider id is required for replace")
                replacement_name = display_name or facade.provider(provider_id).display_name
                catalog = facade.replace_provider(
                    provider_id,
                    display_name=replacement_name,
                    provider_type=provider_type,
                    protocol=protocol,
                    model=model,
                    base_url=base_url,
                    api_path=api_path,
                    api_key=api_key,
                    api_key_env=api_key_env,
                    timeout=timeout,
                    headers=headers,
                    provider_options=provider_options,
                    retry_max_attempts=retry_max_attempts,
                    retry_initial_backoff_seconds=retry_initial_backoff_seconds,
                    retry_max_backoff_seconds=retry_max_backoff_seconds,
                    retry_backoff_multiplier=retry_backoff_multiplier,
                    enabled=configured_enabled,
                    make_default=make_default,
                )
        changed_provider_id = catalog.changed_provider_id
        assert changed_provider_id is not None
        provider = next(
            item for item in catalog.providers if item.provider_id == changed_provider_id
        )
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = _provider_to_public(provider)
    if json_mode:
        template_update = (
            catalog.template_update.model_dump(mode="json", exclude_none=True)
            if catalog.template_update is not None
            else None
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "operation": operation,
                    "config_path": catalog.config_path,
                    "provider": public,
                    "template_update": template_update,
                },
                ensure_ascii=False,
            )
        )
        return 0
    suffix = " and set as default" if public["default"] else ""
    action = {"add": "added", "update": "updated", "replace": "replaced"}[operation]
    print(f"Provider {action}: {provider.display_name} ({provider.provider_id}){suffix}")
    if provider.notice:
        print(provider.notice)
    if catalog.template_update is not None:
        print(_template_update_summary(catalog.template_update))
    return 0


def use_provider_config(
    provider_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        catalog = ConfigurationFacade(home).use_provider(provider_id)
        provider = next(item for item in catalog.providers if item.provider_id == provider_id)
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = _provider_to_public(provider)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "default": provider.provider_id,
                    "config_path": catalog.config_path,
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
        catalog = ConfigurationFacade(home).remove_provider(provider_id)
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "removed": provider_id,
                    "default": catalog.default_provider_id,
                    "config_path": catalog.config_path,
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
        catalog = ConfigurationFacade(home).endpoint_catalog(provider_id)
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    endpoints = [_endpoint_to_public(endpoint) for endpoint in catalog.endpoints]
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "provider_id": catalog.provider_id,
                    "default_endpoint": catalog.default_endpoint,
                    "endpoints": endpoints,
                },
                ensure_ascii=False,
            )
        )
        return 0

    lines = [
        f"Endpoints for {catalog.provider_display_name} ({catalog.provider_id}) "
        f"(default: {catalog.default_endpoint or '-'}):"
    ]
    for endpoint in endpoints:
        marker = "*" if endpoint["default"] else "-"
        lines.append(
            f"{marker} {endpoint.get('display_name') or endpoint['model_id']} "
            f"({endpoint['endpoint_id']}) model={endpoint['model_id']} "
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
        endpoint = ConfigurationFacade(home).endpoint(provider_id, endpoint_id)
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = _endpoint_to_public(endpoint)
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
    lines = [f"Endpoint: {provider_id}/{endpoint.endpoint_id}"]
    for key, value in public.items():
        if key not in {"endpoint_id", "request_options", "metadata"}:
            lines.append(f"{key}: {value}")
    lines.append(
        "request_options: " + json.dumps(public.get("request_options", {}), ensure_ascii=False)
    )
    print("\n".join(lines))
    return 0


def create_provider_endpoint(
    provider_id: str,
    *,
    home: str | Path | None = None,
    display_name: str | None = None,
    model: str,
    base_url: str | None = None,
    api_path: str | None = None,
    temperature: float | None = None,
    context_window: int | None = None,
    max_output_tokens: int | None = None,
    supports_tools: bool | None = None,
    supports_image_input: bool | None = None,
    supports_audio_input: bool | None = None,
    supports_video_input: bool | None = None,
    supports_streaming: bool | None = None,
    supports_structured_output: bool | None = None,
    reasoning: EndpointReasoningConfig | None = None,
    request_options: Mapping[str, Any] | None = None,
    make_default: bool = False,
    json_mode: bool = False,
) -> int:
    try:
        catalog = ConfigurationFacade(home).add_endpoint(
            provider_id,
            display_name=display_name,
            model=model,
            base_url=base_url,
            api_path=api_path,
            temperature=temperature,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            supports_tools=supports_tools,
            supports_image_input=supports_image_input,
            supports_audio_input=supports_audio_input,
            supports_video_input=supports_video_input,
            supports_streaming=supports_streaming,
            supports_structured_output=supports_structured_output,
            reasoning=reasoning,
            request_options=request_options,
            make_default=make_default,
        )
        endpoint_id = catalog.changed_endpoint_id
        assert endpoint_id is not None
        endpoint = next(item for item in catalog.endpoints if item.endpoint_id == endpoint_id)
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = _endpoint_to_public(endpoint)
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
    print(
        f"Endpoint added: {endpoint.display_name or endpoint.model_id} "
        f"({provider_id}/{endpoint_id})"
    )
    return 0


def update_provider_endpoint(
    provider_id: str,
    endpoint_id: str,
    *,
    home: str | Path | None = None,
    display_name: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_path: str | None = None,
    temperature: float | None = None,
    context_window: int | None = None,
    max_output_tokens: int | None = None,
    supports_tools: bool | None = None,
    supports_image_input: bool | None = None,
    supports_audio_input: bool | None = None,
    supports_video_input: bool | None = None,
    supports_streaming: bool | None = None,
    supports_structured_output: bool | None = None,
    reasoning: EndpointReasoningConfig | None = None,
    request_options: Mapping[str, Any] | None = None,
    make_default: bool = False,
    json_mode: bool = False,
) -> int:
    try:
        facade = ConfigurationFacade(home)
        current = facade.endpoint(provider_id, endpoint_id)
        catalog = facade.update_endpoint(
            provider_id,
            endpoint_id,
            display_name=(display_name if display_name is not None else current.display_name),
            model=model,
            base_url=base_url,
            api_path=api_path,
            temperature=temperature,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            supports_tools=supports_tools,
            supports_image_input=supports_image_input,
            supports_audio_input=supports_audio_input,
            supports_video_input=supports_video_input,
            supports_streaming=supports_streaming,
            supports_structured_output=supports_structured_output,
            reasoning=reasoning,
            request_options=request_options,
            make_default=make_default,
        )
        endpoint = next(item for item in catalog.endpoints if item.endpoint_id == endpoint_id)
    except OuroError as exc:
        _print_provider_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = _endpoint_to_public(endpoint)
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
    print(
        f"Endpoint updated: {endpoint.display_name or endpoint.model_id} "
        f"({provider_id}/{endpoint_id})"
    )
    return 0


def use_provider_endpoint(
    provider_id: str,
    endpoint_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        catalog = ConfigurationFacade(home).use_endpoint(provider_id, endpoint_id)
        endpoint = next(item for item in catalog.endpoints if item.endpoint_id == endpoint_id)
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
        facade = ConfigurationFacade(home)
        catalog = facade.remove_endpoint(provider_id, endpoint_id)
        providers = facade.provider_catalog()
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
                    "default_endpoint": catalog.default_endpoint,
                    "default_provider": providers.default_provider_id,
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(
        f"Endpoint removed: {provider_id}/{endpoint_id} "
        f"(default provider: {providers.default_provider_id or '-'})"
    )
    return 0


def _provider_to_public(provider: ProviderView) -> dict[str, Any]:
    return {
        "provider_id": provider.provider_id,
        "display_name": provider.display_name,
        "provider_type": provider.provider_type,
        "protocol": provider.protocol,
        "auth_driver": provider.auth_driver,
        "notice": provider.notice,
        "credential": provider.credential.reference,
        "enabled": provider.enabled,
        "timeout": provider.timeout,
        "base_url": provider.base_url,
        "api_path": provider.api_path,
        "headers": provider.headers,
        "provider_options": provider.provider_options,
        "default": provider.default,
        "default_endpoint": provider.default_endpoint,
        "endpoint_count": provider.endpoint_count,
        "endpoints": [_endpoint_to_public(endpoint) for endpoint in provider.endpoints],
        "template": (
            {
                "template_id": provider.template_id,
                "origin": provider.template_origin,
                "revision": provider.template_revision,
                "tracked": provider.template_revision is not None,
            }
            if provider.template_id is not None
            else None
        ),
    }


def _endpoint_to_public(endpoint: ProviderEndpointView) -> dict[str, Any]:
    return endpoint.model_dump(mode="json", exclude_none=True)


def _template_to_public(template: ProviderTemplateView) -> dict[str, Any]:
    public = template.model_dump(mode="json", exclude_none=True)
    public["endpoint_count"] = template.endpoint_count
    return public


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


def _template_update_summary(update: ProviderTemplateUpdateView) -> str:
    mode = f"r{update.from_revision} -> r{update.to_revision}"
    counts = (
        f"{len(update.added_endpoints)} added",
        f"{len(update.updated_endpoints)} updated",
        f"{len(update.removed_endpoints)} removed",
        f"{len(update.preserved_endpoints)} preserved",
        f"{len(update.conflicted_endpoints)} conflicts",
    )
    return f"Template {update.template_id} {mode}: " + ", ".join(counts)


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
