from __future__ import annotations

import json
import sys
from pathlib import Path

from ouro.cli.errors import error_exit_code
from ouro.config import (
    CompactionConfigManager,
    PermissionConfigManager,
    PromptConfigManager,
    RunBudgetConfigManager,
    ToolExecutionConfigManager,
    compaction_config_to_public_dict,
    permission_config_to_public_dict,
    prompt_config_to_public_dict,
    run_budget_to_public_dict,
    tool_execution_config_to_public_dict,
)
from ouro.core.errors import OuroError
from ouro.core.provider import ThinkingLevel


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


def show_permissions_config(
    *,
    home: str | Path | None = None,
    tool_name: str | None = None,
    mode: str | None = None,
    json_mode: bool = False,
) -> int:
    try:
        manager = PermissionConfigManager(home)
        if tool_name is not None and mode is not None:
            snapshot = manager.set_tool_mode(tool_name, mode)  # type: ignore[arg-type]
        elif tool_name is not None and mode is None:
            snapshot = manager.clear_tool_mode(tool_name)
        else:
            snapshot = manager.snapshot()
    except OuroError as exc:
        _print_config_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    **permission_config_to_public_dict(snapshot),
                },
                ensure_ascii=False,
            )
        )
        return 0

    if not snapshot.tools:
        print("No tool permission overrides configured.")
        return 0

    print("Tool permission overrides:")
    for name, mode_value in sorted(snapshot.tools.items()):
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
        manager = CompactionConfigManager(home)
        config = manager.snapshot()
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
            config = manager.update_model(
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

    public = compaction_config_to_public_dict(config)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "config_path": str(manager.file.path),
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
                f"Config: {manager.file.path}",
            ]
        )
    )
    return 0


def show_tool_execution_config(
    *,
    home: str | Path | None = None,
    mode: str | None = None,
    max_concurrency: int | None = None,
    json_mode: bool = False,
) -> int:
    try:
        manager = ToolExecutionConfigManager(home)
        config = (
            manager.update(mode=mode, max_concurrency=max_concurrency)  # type: ignore[arg-type]
            if mode is not None or max_concurrency is not None
            else manager.snapshot()
        )
    except OuroError as exc:
        _print_config_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = tool_execution_config_to_public_dict(config)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "config_path": str(manager.file.path),
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
                f"Config: {manager.file.path}",
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
        manager = RunBudgetConfigManager(home)
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
            manager.update(
                max_model_rounds=max_model_rounds,
                max_input_tokens=max_input_tokens,
                max_output_tokens=max_output_tokens,
                max_elapsed_seconds=max_elapsed_seconds,
                clear=clear,
            )
            if has_update
            else manager.snapshot()
        )
    except OuroError as exc:
        _print_config_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    public = run_budget_to_public_dict(policy)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "config_path": str(manager.file.path),
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
                f"Config: {manager.file.path}",
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
