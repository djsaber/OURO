from __future__ import annotations

import json
import sys
from pathlib import Path

from ouro.application.configuration import ConfigurationFacade
from ouro.cli.errors import error_exit_code
from ouro.config import QueuedInputConsumptionMode
from ouro.core.errors import OuroError
from ouro.core.provider import ThinkingLevel
from ouro.core.tool_batches import ToolExecutionMode
from ouro.core.tools import PermissionMode


def show_system_prompt_config(
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        config = ConfigurationFacade(home).system_prompt()
    except OuroError as exc:
        _print_config_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = config.model_dump(exclude={"config_path", "change"}, exclude_none=True)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "config_path": config.config_path,
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
                f"Config: {config.config_path}",
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
        config = ConfigurationFacade(home).set_system_prompt(prompt)
    except OuroError as exc:
        _print_config_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "config_path": config.config_path,
                    **config.model_dump(exclude={"config_path", "change"}),
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(f"System prompt saved: {config.config_path}")
    return 0


def clear_system_prompt_config(
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        config = ConfigurationFacade(home).clear_system_prompt()
    except OuroError as exc:
        _print_config_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "config_path": config.config_path,
                    **config.model_dump(exclude={"config_path", "change"}),
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(f"System prompt cleared: {config.config_path}")
    return 0


def show_permissions_config(
    *,
    home: str | Path | None = None,
    tool_name: str | None = None,
    mode: PermissionMode | None = None,
    json_mode: bool = False,
) -> int:
    try:
        facade = ConfigurationFacade(home)
        if tool_name is not None and mode is not None:
            config = facade.set_tool_permission(tool_name, mode)
        elif tool_name is not None and mode is None:
            config = facade.clear_tool_permission(tool_name)
        else:
            config = facade.permission_config()
    except OuroError as exc:
        _print_config_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    **config.model_dump(exclude={"change"}),
                },
                ensure_ascii=False,
            )
        )
        return 0

    if not config.tools:
        print("No tool permission overrides configured.")
        return 0

    print("Tool permission overrides:")
    for name, mode_value in sorted(config.tools.items()):
        print(f"- {name}: {mode_value}")
    return 0


def show_compaction_config(
    *,
    home: str | Path | None = None,
    provider_id: str | None = None,
    endpoint_id: str | None = None,
    runtime_endpoint: bool = False,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    thinking_enabled: bool | None = None,
    thinking_level: ThinkingLevel | None = None,
    clear: bool = False,
    json_mode: bool = False,
) -> int:
    try:
        facade = ConfigurationFacade(home)
        config = facade.compaction_config()
        has_update = any(
            (
                provider_id is not None,
                endpoint_id is not None,
                runtime_endpoint,
                temperature is not None,
                max_output_tokens is not None,
                thinking_enabled is not None,
                thinking_level is not None,
                clear,
            )
        )
        if has_update:
            config = facade.update_compaction_model(
                provider_id=provider_id,
                endpoint_id=endpoint_id,
                use_runtime_endpoint=runtime_endpoint,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                thinking_enabled=thinking_enabled,
                thinking_level=thinking_level,
                clear=clear,
            )
    except OuroError as exc:
        _print_config_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = config.model_dump(exclude={"config_path", "change"}, exclude_none=True)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "config_path": config.config_path,
                    "compaction": public,
                },
                ensure_ascii=False,
            )
        )
        return 0

    model = config.model
    route = (
        f"{model.provider_id}/{model.endpoint_id}"
        if model.has_dedicated_endpoint
        else "session runtime endpoint"
    )
    print(
        "\n".join(
            [
                f"Compaction model route: {route}",
                f"Temperature: {model.temperature if model.temperature is not None else '-'}",
                "Max output tokens: "
                f"{model.max_output_tokens if model.max_output_tokens is not None else '-'}",
                "Thinking enabled: "
                f"{model.thinking_enabled if model.thinking_enabled is not None else '-'}",
                f"Thinking level: {model.thinking_level or '-'}",
                f"Config: {config.config_path}",
            ]
        )
    )
    return 0


def show_tool_execution_config(
    *,
    home: str | Path | None = None,
    mode: ToolExecutionMode | None = None,
    max_concurrency: int | None = None,
    workspace_only: bool | None = None,
    max_output_chars: int | None = None,
    json_mode: bool = False,
) -> int:
    try:
        facade = ConfigurationFacade(home)
        config = (
            facade.update_tool_execution(
                mode=mode,
                max_concurrency=max_concurrency,
                workspace_only=workspace_only,
                max_output_chars=max_output_chars,
            )
            if any(
                value is not None
                for value in (mode, max_concurrency, workspace_only, max_output_chars)
            )
            else facade.tool_execution_config()
        )
    except OuroError as exc:
        _print_config_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = config.model_dump(exclude={"config_path", "change"})
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "config_path": config.config_path,
                    "tool_execution": public,
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(
        "\n".join(
            [
                f"Tool execution mode: {config.mode}",
                f"Max concurrency: {config.max_concurrency}",
                f"Workspace only: {'yes' if config.workspace_only else 'no'}",
                f"Max output chars: {config.max_output_chars}",
                f"Config: {config.config_path}",
            ]
        )
    )
    return 0


def show_queued_input_config(
    *,
    home: str | Path | None = None,
    consumption_mode: QueuedInputConsumptionMode | None = None,
    json_mode: bool = False,
) -> int:
    try:
        facade = ConfigurationFacade(home)
        config = (
            facade.update_queued_inputs(consumption_mode=consumption_mode)
            if consumption_mode is not None
            else facade.queued_input_config()
        )
    except OuroError as exc:
        _print_config_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = config.model_dump(exclude={"config_path", "change"})
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "config_path": config.config_path,
                    "queued_inputs": public,
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(
        "\n".join(
            [
                f"Queued input consumption mode: {config.consumption_mode}",
                f"Config: {config.config_path}",
            ]
        )
    )
    return 0


def show_run_budget_config(
    *,
    home: str | Path | None = None,
    max_model_rounds: int | None = None,
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
    max_elapsed_seconds: float | None = None,
    clear: bool = False,
    json_mode: bool = False,
) -> int:
    try:
        facade = ConfigurationFacade(home)
        has_update = clear or any(
            value is not None
            for value in (
                max_model_rounds,
                max_input_tokens,
                max_output_tokens,
                max_elapsed_seconds,
            )
        )
        policy = (
            facade.update_run_budget(
                max_model_rounds=max_model_rounds,
                max_input_tokens=max_input_tokens,
                max_output_tokens=max_output_tokens,
                max_elapsed_seconds=max_elapsed_seconds,
                clear=clear,
            )
            if has_update
            else facade.run_budget_config()
        )
    except OuroError as exc:
        _print_config_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = policy.model_dump(exclude={"config_path", "change"}, exclude_none=True)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "config_path": policy.config_path,
                    "run_budget": public,
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(
        "\n".join(
            [
                "Run soft budget:",
                f"- max model rounds: {policy.max_model_rounds or 'unlimited'}",
                f"- max input tokens: {policy.max_input_tokens or 'unlimited'}",
                f"- max output tokens: {policy.max_output_tokens or 'unlimited'}",
                f"- max elapsed seconds: {policy.max_elapsed_seconds or 'unlimited'}",
                f"Config: {policy.config_path}",
            ]
        )
    )
    return 0


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
