from __future__ import annotations

import argparse
import json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ouro")
    parser.add_argument("--home", help="Override OURO_HOME")
    parser.add_argument("--system-prompt", help="Override the default system prompt")
    _add_provider_args(parser)
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run a single prompt")
    mode = run_parser.add_mutually_exclusive_group()
    mode.add_argument("--print", dest="print_mode", action="store_true", help="Print final text")
    mode.add_argument("--json", dest="json_mode", action="store_true", help="Print JSON result")
    run_parser.add_argument("--home", dest="run_home", help="Override OURO_HOME")
    run_parser.add_argument("--session", dest="session_id", help="Continue an existing session")
    run_parser.add_argument("--title", help="Title for a new session")
    run_parser.add_argument(
        "--system-prompt",
        dest="run_system_prompt",
        help="Override the default system prompt",
    )
    _add_provider_args(run_parser, prefix="run_")
    run_parser.add_argument("prompt", nargs="+")

    config_parser = subparsers.add_parser("config", help="Inspect configuration")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)
    check_parser = config_subparsers.add_parser("check", help="Check provider connection")
    check_parser.add_argument("--home", dest="check_home", help="Override OURO_HOME")
    check_parser.add_argument("--json", dest="check_json", action="store_true", help="Print JSON")
    _add_provider_args(check_parser, prefix="check_")

    prompt_parser = config_subparsers.add_parser("prompt", help="Inspect or set prompts")
    prompt_parser.add_argument("--home", dest="prompt_home", help="Override OURO_HOME")
    prompt_parser.add_argument("--json", dest="prompt_json", action="store_true", help="Print JSON")
    prompt_mode = prompt_parser.add_mutually_exclusive_group()
    prompt_mode.add_argument(
        "--set",
        dest="prompt_value",
        help="Persist a system prompt override",
    )
    prompt_mode.add_argument(
        "--clear",
        dest="prompt_clear",
        action="store_true",
        help="Clear the persisted system prompt override",
    )

    provider_parser = subparsers.add_parser("provider", help="Manage providers")
    provider_subparsers = provider_parser.add_subparsers(
        dest="provider_command",
        required=True,
    )

    provider_list_parser = provider_subparsers.add_parser("list", help="List providers")
    provider_list_parser.add_argument("--home", dest="provider_home", help="Override OURO_HOME")
    provider_list_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_show_parser = provider_subparsers.add_parser("show", help="Show a provider")
    provider_show_parser.add_argument("provider_id", nargs="?", help="Provider/supplier id")
    provider_show_parser.add_argument("--home", dest="provider_home", help="Override OURO_HOME")
    provider_show_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_set_parser = provider_subparsers.add_parser("set", help="Create or update a provider")
    provider_set_parser.add_argument("provider_id", help="Provider/supplier id")
    provider_set_parser.add_argument("--home", dest="provider_home", help="Override OURO_HOME")
    provider_set_parser.add_argument(
        "--kind",
        choices=["faux", "openai-compatible"],
        default=None,
        help="Provider kind",
    )
    provider_set_parser.add_argument("--model", help="Default model id")
    provider_set_parser.add_argument("--base-url", help="Provider base URL")
    provider_set_parser.add_argument("--api-key", help="Inline API key stored in local config")
    provider_set_parser.add_argument("--api-key-env", help="API key env var")
    provider_set_parser.add_argument("--timeout", type=float, default=None, help="Timeout seconds")
    provider_set_parser.add_argument(
        "--enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable provider",
    )
    provider_set_parser.add_argument(
        "--default",
        action="store_true",
        help="Set provider as default",
    )
    provider_set_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_use_parser = provider_subparsers.add_parser("use", help="Set default provider")
    provider_use_parser.add_argument("provider_id", help="Provider/supplier id")
    provider_use_parser.add_argument("--home", dest="provider_home", help="Override OURO_HOME")
    provider_use_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_remove_parser = provider_subparsers.add_parser("remove", help="Remove a provider")
    provider_remove_parser.add_argument("provider_id", help="Provider/supplier id")
    provider_remove_parser.add_argument("--home", dest="provider_home", help="Override OURO_HOME")
    provider_remove_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_check_parser = provider_subparsers.add_parser(
        "check",
        help="Check provider connection",
    )
    provider_check_parser.add_argument("provider_id", nargs="?", help="Provider/supplier id")
    provider_check_parser.add_argument("--home", dest="provider_home", help="Override OURO_HOME")
    provider_check_parser.add_argument("--json", action="store_true", help="Print JSON")
    _add_provider_args(provider_check_parser, prefix="provider_check_")

    endpoint_parser = provider_subparsers.add_parser(
        "endpoint",
        help="Manage model endpoints for a provider",
    )
    endpoint_subparsers = endpoint_parser.add_subparsers(
        dest="endpoint_command",
        required=True,
    )

    endpoint_list_parser = endpoint_subparsers.add_parser("list", help="List endpoints")
    endpoint_list_parser.add_argument("provider_id", help="Provider/supplier id")
    endpoint_list_parser.add_argument("--home", dest="provider_home", help="Override OURO_HOME")
    endpoint_list_parser.add_argument("--json", action="store_true", help="Print JSON")

    endpoint_show_parser = endpoint_subparsers.add_parser("show", help="Show an endpoint")
    endpoint_show_parser.add_argument("provider_id", help="Provider/supplier id")
    endpoint_show_parser.add_argument("endpoint_id", nargs="?", help="Endpoint id")
    endpoint_show_parser.add_argument("--home", dest="provider_home", help="Override OURO_HOME")
    endpoint_show_parser.add_argument("--json", action="store_true", help="Print JSON")

    endpoint_set_parser = endpoint_subparsers.add_parser(
        "set",
        help="Create or update an endpoint",
    )
    endpoint_set_parser.add_argument("provider_id", help="Provider/supplier id")
    endpoint_set_parser.add_argument("endpoint_id", help="Stable endpoint id")
    endpoint_set_parser.add_argument("--home", dest="provider_home", help="Override OURO_HOME")
    endpoint_set_parser.add_argument("--model", help="Provider model id")
    endpoint_set_parser.add_argument("--base-url", help="Endpoint base URL")
    endpoint_set_parser.add_argument("--api-path", help="API path, default /chat/completions")
    endpoint_set_parser.add_argument("--temperature", type=float, default=None)
    endpoint_set_parser.add_argument("--context-window", type=int, default=None)
    endpoint_set_parser.add_argument("--max-output-tokens", type=int, default=None)
    _add_endpoint_capability_args(endpoint_set_parser)
    endpoint_set_parser.add_argument(
        "--thinking-levels",
        nargs="+",
        choices=_THINKING_LEVELS,
        default=None,
        help="Supported thinking levels",
    )
    endpoint_set_parser.add_argument(
        "--thinking-level",
        choices=_THINKING_LEVELS,
        default=None,
        help="Default thinking level",
    )
    endpoint_set_parser.add_argument(
        "--request-options",
        type=_json_object,
        help="Provider-specific request options as a JSON object",
    )
    endpoint_set_parser.add_argument(
        "--default",
        action="store_true",
        help="Set endpoint as provider default",
    )
    endpoint_set_parser.add_argument("--json", action="store_true", help="Print JSON")

    endpoint_use_parser = endpoint_subparsers.add_parser("use", help="Set default endpoint")
    endpoint_use_parser.add_argument("provider_id", help="Provider/supplier id")
    endpoint_use_parser.add_argument("endpoint_id", help="Endpoint id")
    endpoint_use_parser.add_argument("--home", dest="provider_home", help="Override OURO_HOME")
    endpoint_use_parser.add_argument("--json", action="store_true", help="Print JSON")

    endpoint_remove_parser = endpoint_subparsers.add_parser("remove", help="Remove an endpoint")
    endpoint_remove_parser.add_argument("provider_id", help="Provider/supplier id")
    endpoint_remove_parser.add_argument("endpoint_id", help="Endpoint id")
    endpoint_remove_parser.add_argument("--home", dest="provider_home", help="Override OURO_HOME")
    endpoint_remove_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_parser = subparsers.add_parser("skills", help="Manage Agent Skills")
    skills_subparsers = skills_parser.add_subparsers(dest="skills_command", required=True)

    skills_list_parser = skills_subparsers.add_parser("list", help="List available skills")
    skills_list_parser.add_argument("--home", dest="skills_home", help="Override OURO_HOME")
    skills_list_parser.add_argument("--json", action="store_true", help="Print JSON")
    skills_list_parser.add_argument(
        "--all",
        dest="skills_all",
        action="store_true",
        help="Include disabled skills",
    )

    skills_show_parser = skills_subparsers.add_parser("show", help="Show a skill")
    skills_show_parser.add_argument("skill_id", help="Skill id")
    skills_show_parser.add_argument("--home", dest="skills_home", help="Override OURO_HOME")
    skills_show_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_roots_parser = skills_subparsers.add_parser("roots", help="Show skill search roots")
    skills_roots_parser.add_argument("--home", dest="skills_home", help="Override OURO_HOME")
    skills_roots_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_prompt_parser = skills_subparsers.add_parser(
        "prompt",
        help="Preview injected skills prompt",
    )
    skills_prompt_parser.add_argument("--home", dest="skills_home", help="Override OURO_HOME")
    skills_prompt_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_validate_parser = skills_subparsers.add_parser(
        "validate",
        help="Validate one skill, a skill collection, or all discovered skills",
    )
    skills_validate_parser.add_argument("path", nargs="?", help="SKILL.md or directory")
    skills_validate_parser.add_argument("--home", dest="skills_home", help="Override OURO_HOME")
    skills_validate_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_install_parser = skills_subparsers.add_parser(
        "install",
        help="Install skills from a local directory or git URL",
    )
    skills_install_parser.add_argument("source", help="Local path, git URL, or github:owner/repo")
    skills_install_parser.add_argument("--home", dest="skills_home", help="Override OURO_HOME")
    skills_install_parser.add_argument(
        "--scope",
        choices=["user", "workspace"],
        default="user",
        help="Install into OURO_HOME/skills or ./.ouro/skills",
    )
    install_selection = skills_install_parser.add_mutually_exclusive_group()
    install_selection.add_argument("--skill", dest="skill_selector", help="Select one skill")
    install_selection.add_argument(
        "--all",
        dest="install_all",
        action="store_true",
        help="Install every valid skill in the source",
    )
    skills_install_parser.add_argument("--ref", help="Git branch or tag")
    skills_install_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing skill atomically",
    )
    skills_install_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_update_parser = skills_subparsers.add_parser(
        "update",
        help="Update an Ouro-managed skill from its recorded source",
    )
    skills_update_parser.add_argument("skill_id", help="Skill name")
    skills_update_parser.add_argument("--home", dest="skills_home", help="Override OURO_HOME")
    skills_update_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_remove_parser = skills_subparsers.add_parser(
        "remove",
        aliases=["uninstall"],
        help="Remove an Ouro-managed skill",
    )
    skills_remove_parser.add_argument("skill_id", help="Skill name")
    skills_remove_parser.add_argument("--home", dest="skills_home", help="Override OURO_HOME")
    skills_remove_parser.add_argument(
        "--force",
        action="store_true",
        help="Remove an unregistered skill from an Ouro-native skill directory",
    )
    skills_remove_parser.add_argument("--json", action="store_true", help="Print JSON")

    for command, help_text in (
        ("enable", "Enable a discovered skill"),
        ("disable", "Disable a discovered skill"),
    ):
        command_parser = skills_subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("skill_id", help="Skill name")
        command_parser.add_argument("--home", dest="skills_home", help="Override OURO_HOME")
        command_parser.add_argument("--json", action="store_true", help="Print JSON")

    return parser


def _add_provider_args(parser: argparse.ArgumentParser, *, prefix: str = "") -> None:
    parser.add_argument(
        "--provider",
        dest=f"{prefix}provider",
        choices=["faux", "openai-compatible"],
        default=None,
        help="Provider kind",
    )
    parser.add_argument("--provider-id", dest=f"{prefix}provider_id", help="Provider/supplier id")
    parser.add_argument("--endpoint", dest=f"{prefix}endpoint_id", help="Configured endpoint id")
    parser.add_argument("--model", dest=f"{prefix}model", help="Model id")
    parser.add_argument("--base-url", dest=f"{prefix}base_url", help="Provider base URL")
    parser.add_argument("--api-path", dest=f"{prefix}api_path", help="Provider API path")
    parser.add_argument("--api-key", dest=f"{prefix}api_key", help="Inline API key")
    parser.add_argument("--api-key-env", dest=f"{prefix}api_key_env", help="API key env var")
    parser.add_argument(
        "--timeout",
        dest=f"{prefix}timeout",
        type=float,
        default=None,
        help="Provider timeout seconds",
    )
    parser.add_argument(
        "--temperature",
        dest=f"{prefix}temperature",
        type=float,
        default=None,
        help="Sampling temperature",
    )
    parser.add_argument(
        "--context-window",
        dest=f"{prefix}context_window",
        type=int,
        default=None,
        help="Endpoint context window",
    )
    parser.add_argument(
        "--max-output-tokens",
        dest=f"{prefix}max_output_tokens",
        type=int,
        default=None,
        help="Endpoint maximum output tokens",
    )
    _add_endpoint_capability_args(parser, prefix=prefix)
    parser.add_argument(
        "--thinking-level",
        dest=f"{prefix}thinking_level",
        choices=_THINKING_LEVELS,
        default=None,
        help="Reasoning/thinking level",
    )


_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


def _add_endpoint_capability_args(
    parser: argparse.ArgumentParser,
    *,
    prefix: str = "",
) -> None:
    for options, dest, help_text in (
        (("tools",), "supports_tools", "tool calling"),
        (("vision", "multimodal"), "supports_vision", "image/multimodal input"),
        (("streaming",), "supports_streaming", "streaming output"),
        (("structured-output",), "supports_structured_output", "structured output"),
        (("reasoning",), "supports_reasoning", "reasoning/thinking controls"),
    ):
        parser.add_argument(
            *(f"--{option}" for option in options),
            dest=f"{prefix}{dest}",
            action=argparse.BooleanOptionalAction,
            default=None,
            help=f"Enable or disable {help_text}",
        )


def _json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("request options must be a JSON object")
    return parsed
