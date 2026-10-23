"""Register mcp command arguments."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any


def _add_mcp_parser(subparsers: argparse._SubParsersAction) -> None:
    mcp_parser = subparsers.add_parser("mcp", help="Manage MCP servers")
    mcp_subparsers = mcp_parser.add_subparsers(dest="mcp_command", required=True)

    list_parser = mcp_subparsers.add_parser("list", help="List MCP servers")
    _add_mcp_common_output_args(list_parser)

    show_parser = mcp_subparsers.add_parser("show", help="Show an MCP server")
    show_parser.add_argument("server_name", help="MCP display name (exact match)")
    _add_mcp_common_output_args(show_parser)

    set_parser = mcp_subparsers.add_parser("set", help="Create or update an MCP server")
    set_parser.add_argument("server_name", help="MCP display name (exact match)")
    set_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    set_parser.add_argument("--name", help="New display name")
    set_parser.add_argument("--transport", choices=("stdio", "http", "sse"))
    set_parser.add_argument(
        "--command",
        dest="mcp_executable",
        help="Executable for stdio transport",
    )
    _add_replaceable_option(
        set_parser,
        "arg",
        "args",
        clear_option="args",
        help_text="stdio argument",
    )
    _add_replaceable_option(
        set_parser,
        "env",
        "env",
        clear_option="env",
        value_type=_key_value,
        metavar="NAME=VALUE",
        help_text="stdio environment entry",
    )
    _add_replaceable_option(
        set_parser,
        "inherit-env",
        "inherit_env",
        clear_option="inherit-env",
        metavar="NAME",
        help_text="inherited stdio environment variable",
    )
    cwd_group = set_parser.add_mutually_exclusive_group()
    cwd_group.add_argument("--cwd", help="Working directory for stdio transport")
    cwd_group.add_argument("--clear-cwd", action="store_true", help="Clear stdio working directory")
    set_parser.add_argument("--url", help="URL for HTTP or SSE transport")
    _add_replaceable_option(
        set_parser,
        "header",
        "headers",
        clear_option="headers",
        value_type=_key_value,
        metavar="NAME=VALUE",
        help_text="remote request header",
    )
    _add_replaceable_option(
        set_parser,
        "credential",
        "credentials",
        clear_option="credentials",
        value_type=_key_value,
        metavar="TARGET=REFERENCE",
        help_text="credential target and env:NAME or inline:VALUE reference",
    )
    set_parser.add_argument(
        "--enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable the server",
    )
    set_parser.add_argument(
        "--auto-start",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Start the server with the runtime",
    )
    set_parser.add_argument(
        "--auto-reconnect",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Reconnect after connection loss",
    )
    set_parser.add_argument("--max-reconnect-attempts", type=int)
    set_parser.add_argument("--reconnect-backoff", type=float, metavar="SECONDS")
    set_parser.add_argument("--timeout", type=float, metavar="SECONDS")
    set_parser.add_argument("--health-check-interval", type=float, metavar="SECONDS")
    set_parser.add_argument("--permission-mode", choices=("auto", "approval", "deny"))
    _add_replaceable_option(
        set_parser,
        "tool-effect",
        "tool_effects",
        clear_option="tool-effects",
        value_type=_tool_effect_override,
        metavar="TOOL=EFFECT",
        help_text="tool effect override (read_only, state_changing, or unknown)",
    )
    set_parser.add_argument("--json", action="store_true", help="Print JSON")

    remove_parser = mcp_subparsers.add_parser("remove", help="Remove an MCP server")
    remove_parser.add_argument("server_name", help="MCP display name (exact match)")
    _add_mcp_common_output_args(remove_parser)

    check_parser = mcp_subparsers.add_parser("check", help="Check an MCP server connection")
    check_parser.add_argument("server_name", help="MCP display name (exact match)")
    _add_mcp_common_output_args(check_parser)


def _add_mcp_common_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    parser.add_argument("--json", action="store_true", help="Print JSON")


def _add_replaceable_option(
    parser: argparse.ArgumentParser,
    option: str,
    destination: str,
    *,
    clear_option: str,
    value_type: Callable[[str], Any] | None = None,
    metavar: str | None = None,
    help_text: str,
) -> None:
    group = parser.add_mutually_exclusive_group()
    option_help = f"Set {help_text}; repeat to replace the complete set"
    if value_type is None:
        group.add_argument(
            f"--{option}",
            dest=destination,
            action="append",
            metavar=metavar,
            help=option_help,
        )
    else:
        group.add_argument(
            f"--{option}",
            dest=destination,
            action="append",
            type=value_type,
            metavar=metavar,
            help=option_help,
        )
    group.add_argument(
        f"--clear-{clear_option}",
        dest=f"clear_{destination}",
        action="store_true",
        help=f"Clear all {help_text} values",
    )


def _key_value(value: str) -> tuple[str, str]:
    key, separator, item = value.partition("=")
    if not separator or not key:
        raise argparse.ArgumentTypeError("expected NAME=VALUE")
    return key, item


def _tool_effect_override(value: str) -> tuple[str, str]:
    tool_name, effect = _key_value(value)
    if effect not in {"read_only", "state_changing", "unknown"}:
        raise argparse.ArgumentTypeError(
            "tool effect must be read_only, state_changing, or unknown"
        )
    return tool_name, effect
