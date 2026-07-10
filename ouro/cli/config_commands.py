from __future__ import annotations

import json
import sys
from pathlib import Path

from ouro.cli.errors import error_exit_code
from ouro.config import (
    PromptConfigManager,
    prompt_config_to_public_dict,
)
from ouro.core.errors import OuroError


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
