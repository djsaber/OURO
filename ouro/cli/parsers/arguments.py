"""Shared CLI backend, workspace and output argument helpers."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable

from ouro.deployment import SESSION_BACKENDS, TASK_BACKENDS


def _add_session_backend_arg(
    parser: argparse.ArgumentParser,
    *,
    dest: str = "command_session_backend",
) -> None:
    parser.add_argument(
        "--session-backend",
        choices=SESSION_BACKENDS,
        dest=dest,
        default=None,
        help="Session storage backend (default: OURO_SESSION_BACKEND or files)",
    )


def _add_task_backend_arg(parser: argparse.ArgumentParser, *, dest: str) -> None:
    parser.add_argument(
        "--task-backend",
        choices=TASK_BACKENDS,
        dest=dest,
        default=None,
        help="Task record storage backend (default: OURO_TASK_BACKEND or files)",
    )


def _canonical_choice(values: dict[str, str]) -> Callable[[str], str]:
    def parse(value: str) -> str:
        try:
            return values[value]
        except KeyError as exc:
            choices = ", ".join(values)
            raise argparse.ArgumentTypeError(f"choose one of: {choices}") from exc

    return parse


def _json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("value must be a JSON object")
    return parsed


def _output_limit(value: str) -> int | None:
    if value.casefold() == "none":
        return None
    try:
        limit = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a positive integer or none") from None
    if limit < 1:
        raise argparse.ArgumentTypeError("expected a positive integer or none")
    return limit
