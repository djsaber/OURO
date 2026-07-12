from __future__ import annotations

import argparse
import json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ouro")
    parser.add_argument("--home", help="Override OURO_HOME")
    parser.add_argument("--system-prompt", help="Override the default system prompt")
    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version="%(prog)s 0.1.0",
        help="Show version and exit",
    )
    subparsers = parser.add_subparsers(dest="command")

    _add_run_parser(subparsers)
    _add_session_parser(subparsers)
    _add_approval_parsers(subparsers)
    _add_config_parser(subparsers)
    _add_provider_parser(subparsers)
    _add_skills_parser(subparsers)

    return parser


def _add_run_parser(subparsers: argparse._SubParsersAction) -> None:
    run_parser = subparsers.add_parser(
        "run",
        help="Run a single prompt",
        description="Run a single prompt. Defaults to --print (final text on stdout).",
    )
    mode = run_parser.add_mutually_exclusive_group()
    mode.add_argument("--print", dest="print_mode", action="store_true", help="Print final text")
    mode.add_argument("--json", dest="json_mode", action="store_true", help="Print JSON result")
    run_parser.add_argument("--home", dest="run_home", metavar="HOME", help="Override OURO_HOME")
    run_parser.add_argument("--session", dest="session_id", help="Continue an existing session")
    run_parser.add_argument("--title", help="Title for a new session")
    run_parser.add_argument(
        "--queue-mode",
        choices=["follow_up", "steer"],
        default="follow_up",
        help="Delivery mode if the target session already has an active run",
    )
    run_parser.add_argument(
        "--system-prompt",
        dest="run_system_prompt",
        help="Override the default system prompt",
    )
    _add_provider_args(run_parser, prefix="run_")
    run_parser.add_argument("prompt", nargs="+")


def _add_session_parser(subparsers: argparse._SubParsersAction) -> None:
    session_parser = subparsers.add_parser("session", help="Manage sessions")
    session_subparsers = session_parser.add_subparsers(
        dest="session_command",
        required=True,
    )

    list_parser = session_subparsers.add_parser("list", help="List sessions")
    list_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    list_parser.add_argument("--json", action="store_true", help="Print JSON")
    list_parser.add_argument("--search", help="Search title, preview, workspace, model, and tags")
    list_parser.add_argument(
        "--status",
        dest="statuses",
        action="append",
        choices=(
            "idle",
            "active",
            "waiting_approval",
            "waiting_user",
            "failed",
            "paused",
            "recoverable",
            "completed",
        ),
        help="Filter by catalog status; repeat to select multiple statuses",
    )
    list_parser.add_argument(
        "--archived",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Filter by archive state",
    )
    list_parser.add_argument(
        "--pinned",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Filter by pinned state",
    )
    list_parser.add_argument("--workspace", help="Filter by exact workspace")
    list_parser.add_argument("--model", help="Filter by exact model route")
    list_parser.add_argument("--tag", dest="tags", action="append", help="Require a tag")
    list_parser.add_argument(
        "--sort",
        choices=("updated_desc", "created_desc", "title"),
        default="updated_desc",
    )

    show_parser = session_subparsers.add_parser("show", help="Show session details")
    show_parser.add_argument("session_id", help="Session id")
    show_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    show_parser.add_argument("--json", action="store_true", help="Print JSON")

    rename_parser = session_subparsers.add_parser("rename", help="Rename a session")
    rename_parser.add_argument("session_id", help="Session id")
    rename_parser.add_argument("title", help="New title")
    rename_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    rename_parser.add_argument("--json", action="store_true", help="Print JSON")

    for command, help_text in (
        ("archive", "Archive a session"),
        ("unarchive", "Restore an archived session to the catalog"),
        ("pin", "Pin a session"),
        ("unpin", "Unpin a session"),
    ):
        state_parser = session_subparsers.add_parser(command, help=help_text)
        state_parser.add_argument("session_id", help="Session id")
        state_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
        state_parser.add_argument("--json", action="store_true", help="Print JSON")

    branch_parser = session_subparsers.add_parser("branch", help="Branch at a message boundary")
    branch_parser.add_argument("session_id", help="Source session id")
    branch_parser.add_argument("message_id", help="Canonical source message id")
    branch_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    branch_parser.add_argument("--json", action="store_true", help="Print JSON")

    replay_parser = session_subparsers.add_parser("replay", help="Project persisted events")
    replay_parser.add_argument("session_id", help="Session id")
    replay_parser.add_argument("--run", dest="run_id", help="Filter by run id")
    replay_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    replay_parser.add_argument("--json", action="store_true", help="Print JSON")

    remove_parser = session_subparsers.add_parser("remove", help="Remove a session")
    remove_parser.add_argument("session_id", help="Session id")
    remove_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    remove_parser.add_argument("--force", action="store_true", help="Skip confirmation")
    remove_parser.add_argument("--json", action="store_true", help="Print JSON")

    export_parser = session_subparsers.add_parser("export", help="Export session data")
    export_parser.add_argument("session_id", help="Session id")
    export_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    export_parser.add_argument(
        "--output",
        help="Write export to a file instead of stdout",
    )
    export_parser.add_argument("--json", action="store_true", help="Print JSON")

    messages_parser = session_subparsers.add_parser("messages", help="Show session messages")
    messages_parser.add_argument("session_id", help="Session id")
    messages_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    messages_parser.add_argument("--json", action="store_true", help="Print JSON")

    events_parser = session_subparsers.add_parser("events", help="Show session events")
    events_parser.add_argument("session_id", help="Session id")
    events_parser.add_argument("--run", dest="run_id", help="Filter by run id")
    events_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    events_parser.add_argument("--json", action="store_true", help="Print JSON")

    compact_parser = session_subparsers.add_parser(
        "compact",
        help="Summarize and compress older session context",
    )
    compact_parser.add_argument("session_id", help="Session id")
    compact_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    compact_parser.add_argument("--json", action="store_true", help="Print JSON")


def _add_approval_parsers(subparsers: argparse._SubParsersAction) -> None:
    approve_parser = subparsers.add_parser(
        "approve",
        help="Approve a pending tool call for a session",
    )
    approve_parser.add_argument("session_id", help="Session id")
    approve_parser.add_argument("--run", dest="run_id", help="Run id (default: active run)")
    approve_parser.add_argument(
        "--modify",
        type=_json_object,
        help="Modified tool arguments as a JSON object",
    )
    approve_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    approve_parser.add_argument("--json", action="store_true", help="Print JSON")

    reject_parser = subparsers.add_parser(
        "reject",
        help="Reject a pending tool call for a session",
    )
    reject_parser.add_argument("session_id", help="Session id")
    reject_parser.add_argument("--run", dest="run_id", help="Run id (default: active run)")
    reject_parser.add_argument("--reason", default="User rejected", help="Rejection reason")
    reject_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    reject_parser.add_argument("--json", action="store_true", help="Print JSON")

    abort_parser = subparsers.add_parser(
        "abort",
        help="Abort the active run for a session",
    )
    abort_parser.add_argument("session_id", help="Session id")
    abort_parser.add_argument("--run", dest="run_id", help="Run id (default: active run)")
    abort_parser.add_argument("--reason", default="User aborted", help="Abort reason")
    abort_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    abort_parser.add_argument("--json", action="store_true", help="Print JSON")


def _add_config_parser(subparsers: argparse._SubParsersAction) -> None:
    config_parser = subparsers.add_parser("config", help="Inspect configuration")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)

    prompt_parser = config_subparsers.add_parser("prompt", help="Inspect or set prompts")
    prompt_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
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

    permissions_parser = config_subparsers.add_parser(
        "permissions",
        help="Inspect or set tool permission modes",
    )
    permissions_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    permissions_parser.add_argument("--tool", help="Tool name")
    permissions_parser.add_argument(
        "--mode",
        choices=["auto", "approval"],
        help="Permission mode for the tool",
    )
    permissions_parser.add_argument("--json", action="store_true", help="Print JSON")

    tool_execution_parser = config_subparsers.add_parser(
        "tool-execution",
        help="Inspect or set tool batch execution policy",
    )
    tool_execution_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    tool_execution_parser.add_argument(
        "--mode",
        choices=["sequential", "parallel"],
        help="Batch execution mode",
    )
    tool_execution_parser.add_argument(
        "--max-concurrency",
        type=int,
        default=None,
        help="Maximum parallel tool calls (1-64)",
    )
    tool_execution_parser.add_argument("--json", action="store_true", help="Print JSON")

    queued_inputs_parser = config_subparsers.add_parser(
        "queued-inputs",
        help="Inspect or set queued input consumption policy",
    )
    queued_inputs_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    queued_inputs_parser.add_argument(
        "--consumption-mode",
        choices=["one_by_one", "all_at_once"],
        help="Send one eligible input per model request or all eligible inputs together",
    )
    queued_inputs_parser.add_argument("--json", action="store_true", help="Print JSON")

    run_budget_parser = config_subparsers.add_parser(
        "run-budget",
        help="Inspect or set optional per-run soft limits",
    )
    run_budget_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    run_budget_parser.add_argument("--max-model-rounds", type=int, default=None)
    run_budget_parser.add_argument("--max-input-tokens", type=int, default=None)
    run_budget_parser.add_argument("--max-output-tokens", type=int, default=None)
    run_budget_parser.add_argument("--max-elapsed-seconds", type=float, default=None)
    run_budget_parser.add_argument(
        "--clear",
        action="store_true",
        help="Remove all run soft limits",
    )
    run_budget_parser.add_argument("--json", action="store_true", help="Print JSON")

    compaction_parser = config_subparsers.add_parser(
        "compaction",
        help="Inspect or set compaction model routing and request parameters",
    )
    compaction_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    compaction_parser.add_argument("--provider", dest="provider_id", help="Provider id")
    compaction_parser.add_argument("--endpoint", dest="endpoint_id", help="Endpoint id")
    compaction_parser.add_argument(
        "--runtime-endpoint",
        action="store_true",
        help="Clear the dedicated endpoint and use the session runtime endpoint",
    )
    compaction_parser.add_argument("--temperature", type=float, default=None)
    compaction_parser.add_argument("--max-output-tokens", type=int, default=None)
    compaction_parser.add_argument(
        "--thinking",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable thinking for compaction requests",
    )
    compaction_parser.add_argument(
        "--thinking-level",
        choices=("off", "minimal", "low", "medium", "high", "xhigh", "max"),
        default=None,
    )
    compaction_parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear the dedicated endpoint and all compaction model overrides",
    )
    compaction_parser.add_argument("--json", action="store_true", help="Print JSON")


def _add_provider_parser(subparsers: argparse._SubParsersAction) -> None:
    provider_parser = subparsers.add_parser("provider", help="Manage providers")
    provider_subparsers = provider_parser.add_subparsers(
        dest="provider_command",
        required=True,
    )

    provider_list_parser = provider_subparsers.add_parser("list", help="List providers")
    provider_list_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    provider_list_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_show_parser = provider_subparsers.add_parser("show", help="Show a provider")
    provider_show_parser.add_argument("provider_id", nargs="?", help="Provider/supplier id")
    provider_show_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    provider_show_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_set_parser = provider_subparsers.add_parser(
        "set",
        help="Create or update a provider",
    )
    provider_set_parser.add_argument("provider_id", help="Provider/supplier id")
    provider_set_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    provider_set_parser.add_argument(
        "--kind",
        choices=["faux", "openai-compatible"],
        default=None,
        help="Provider kind",
    )
    provider_set_parser.add_argument(
        "--base-url",
        help="Provider base URL used as the default for endpoints",
    )
    provider_set_parser.add_argument(
        "--api-path",
        help="Provider API path used as the default for endpoints",
    )
    provider_set_parser.add_argument(
        "--api-key",
        help="Inline API key (stored as plain text in local config; prefer --api-key-env)",
    )
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
    provider_use_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    provider_use_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_remove_parser = provider_subparsers.add_parser("remove", help="Remove a provider")
    provider_remove_parser.add_argument("provider_id", help="Provider/supplier id")
    provider_remove_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    provider_remove_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_check_parser = provider_subparsers.add_parser(
        "check",
        help="Check provider connection",
    )
    provider_check_parser.add_argument("provider_id", nargs="?", help="Provider/supplier id")
    provider_check_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    provider_check_parser.add_argument("--json", action="store_true", help="Print JSON")
    _add_provider_args(
        provider_check_parser,
        prefix="provider_check_",
        include_provider=False,
    )

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
    endpoint_list_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    endpoint_list_parser.add_argument("--json", action="store_true", help="Print JSON")

    endpoint_show_parser = endpoint_subparsers.add_parser("show", help="Show an endpoint")
    endpoint_show_parser.add_argument("provider_id", help="Provider/supplier id")
    endpoint_show_parser.add_argument("endpoint_id", nargs="?", help="Endpoint id")
    endpoint_show_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    endpoint_show_parser.add_argument("--json", action="store_true", help="Print JSON")

    endpoint_set_parser = endpoint_subparsers.add_parser(
        "set",
        help="Create or update an endpoint",
    )
    endpoint_set_parser.add_argument("provider_id", help="Provider/supplier id")
    endpoint_set_parser.add_argument("endpoint_id", help="Stable endpoint id")
    endpoint_set_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    endpoint_set_parser.add_argument("--model", help="Provider model id")
    endpoint_set_parser.add_argument("--base-url", help="Endpoint base URL")
    endpoint_set_parser.add_argument(
        "--api-path",
        help="API path (defaults to provider api_path or kind default)",
    )
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
    endpoint_use_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    endpoint_use_parser.add_argument("--json", action="store_true", help="Print JSON")

    endpoint_remove_parser = endpoint_subparsers.add_parser("remove", help="Remove an endpoint")
    endpoint_remove_parser.add_argument("provider_id", help="Provider/supplier id")
    endpoint_remove_parser.add_argument("endpoint_id", help="Endpoint id")
    endpoint_remove_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    endpoint_remove_parser.add_argument("--json", action="store_true", help="Print JSON")


def _add_skills_parser(subparsers: argparse._SubParsersAction) -> None:
    skills_parser = subparsers.add_parser("skills", help="Manage Agent Skills")
    skills_subparsers = skills_parser.add_subparsers(dest="skills_command", required=True)

    skills_list_parser = skills_subparsers.add_parser("list", help="List available skills")
    skills_list_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    skills_list_parser.add_argument("--json", action="store_true", help="Print JSON")
    skills_list_parser.add_argument(
        "--all",
        dest="skills_all",
        action="store_true",
        help="Include disabled skills",
    )

    skills_show_parser = skills_subparsers.add_parser("show", help="Show a skill")
    skills_show_parser.add_argument("skill_name", help="Skill name")
    skills_show_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    skills_show_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_roots_parser = skills_subparsers.add_parser("roots", help="Show skill search roots")
    skills_roots_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    skills_roots_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_prompt_parser = skills_subparsers.add_parser(
        "prompt",
        help="Preview injected skills prompt",
    )
    skills_prompt_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    skills_prompt_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_validate_parser = skills_subparsers.add_parser(
        "validate",
        help="Validate one skill, a skill collection, or all discovered skills",
    )
    skills_validate_parser.add_argument("path", nargs="?", help="SKILL.md or directory")
    skills_validate_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    skills_validate_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_install_parser = skills_subparsers.add_parser(
        "install",
        help="Install skills from a local directory or git URL",
    )
    skills_install_parser.add_argument("source", help="Local path, git URL, or github:owner/repo")
    skills_install_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
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
    skills_update_parser.add_argument("skill_name", help="Skill name")
    skills_update_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    skills_update_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_remove_parser = skills_subparsers.add_parser(
        "remove",
        help="Remove an Ouro-managed skill",
    )
    skills_remove_parser.add_argument("skill_name", help="Skill name")
    skills_remove_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
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
        command_parser.add_argument("skill_name", help="Skill name")
        command_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
        command_parser.add_argument("--json", action="store_true", help="Print JSON")


def _add_provider_args(
    parser: argparse.ArgumentParser,
    *,
    prefix: str = "",
    include_provider: bool = True,
) -> None:
    if include_provider:
        parser.add_argument(
            "--provider",
            dest=f"{prefix}provider_id",
            default=None,
            metavar="PROVIDER_ID",
            help="Configured provider id",
        )
    parser.add_argument(
        "--endpoint",
        dest=f"{prefix}endpoint_id",
        metavar="ENDPOINT_ID",
        help="Configured endpoint id",
    )
    parser.add_argument(
        "--base-url",
        dest=f"{prefix}base_url",
        metavar="URL",
        help="Provider base URL",
    )
    parser.add_argument(
        "--api-path",
        dest=f"{prefix}api_path",
        metavar="PATH",
        help="Provider API path",
    )
    parser.add_argument(
        "--api-key",
        dest=f"{prefix}api_key",
        metavar="KEY",
        help="Inline API key for this command (prefer --api-key-env)",
    )
    parser.add_argument(
        "--api-key-env",
        dest=f"{prefix}api_key_env",
        metavar="ENV",
        help="API key env var",
    )
    parser.add_argument(
        "--timeout",
        dest=f"{prefix}timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Provider timeout seconds",
    )
    parser.add_argument(
        "--temperature",
        dest=f"{prefix}temperature",
        type=float,
        default=None,
        metavar="FLOAT",
        help="Sampling temperature",
    )
    parser.add_argument(
        "--context-window",
        dest=f"{prefix}context_window",
        type=int,
        default=None,
        metavar="INT",
        help="Endpoint context window",
    )
    parser.add_argument(
        "--max-output-tokens",
        dest=f"{prefix}max_output_tokens",
        type=int,
        default=None,
        metavar="INT",
        help="Endpoint maximum output tokens",
    )
    _add_endpoint_capability_args(parser, prefix=prefix)
    parser.add_argument(
        "--thinking-level",
        dest=f"{prefix}thinking_level",
        choices=_THINKING_LEVELS,
        default=None,
        metavar="LEVEL",
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
        (("vision",), "supports_vision", "image/multimodal input"),
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
