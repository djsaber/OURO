"""Register configuration command arguments."""

from __future__ import annotations

import argparse

from ouro.cli.parsers.arguments import _canonical_choice, _output_limit
from ouro.config.skills import SKILL_DISCOVERY_SOURCE_IDS


def _add_config_parser(subparsers: argparse._SubParsersAction) -> None:
    config_parser = subparsers.add_parser("config", help="Inspect or update configuration")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)

    prompt_parser = config_subparsers.add_parser("prompt", help="Manage personal system prompts")
    prompt_parser.add_argument("--home", help="Override OURO_HOME")
    prompt_parser.add_argument("--json", action="store_true")
    prompt_subparsers = prompt_parser.add_subparsers(dest="prompt_command")
    for action in ("list", "add", "edit", "remove", "enable", "disable", "move"):
        command = prompt_subparsers.add_parser(action)
        command.add_argument("--home", default=argparse.SUPPRESS, help="Override OURO_HOME")
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if action == "list":
            continue
        command.add_argument("kind", choices=("replacement", "addition"))
        if action != "add":
            command.add_argument("number", type=int, help="Prompt number shown in the list")
        command.add_argument(
            "--expected-revision", type=int, help="Reject stale configuration edits"
        )
        if action in {"add", "edit"}:
            command.add_argument("--description", help="Optional display-only annotation")
            content = command.add_mutually_exclusive_group(required=action == "add")
            content.add_argument("--text", help="Prompt content")
            content.add_argument("--file", help="Read prompt content from a UTF-8 file")
            enabled = command.add_mutually_exclusive_group()
            enabled.add_argument("--enabled", dest="enabled", action="store_true", default=None)
            enabled.add_argument("--disabled", dest="enabled", action="store_false")
        if action == "move":
            command.add_argument("position", type=int, help="New position, starting at 1")

    permissions_parser = config_subparsers.add_parser(
        "permissions",
        help="Inspect or set tool and plugin permission modes",
    )
    permissions_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    permission_target = permissions_parser.add_mutually_exclusive_group()
    permission_target.add_argument("--tool", help="Tool name")
    permission_target.add_argument("--plugin", help="Plugin id")
    permissions_parser.add_argument(
        "--mode",
        choices=["auto", "approval", "deny"],
        help="Permission mode; omit it to clear the selected override",
    )
    permissions_parser.add_argument("--json", action="store_true", help="Print JSON")

    skills_parser = config_subparsers.add_parser(
        "skills",
        help="Inspect or update Host skill directory discovery",
    )
    skills_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    skills_parser.add_argument(
        "--discovery-enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable all Host directory skill discovery",
    )
    skills_parser.add_argument(
        "--enable-source",
        action="append",
        choices=SKILL_DISCOVERY_SOURCE_IDS,
        default=None,
        metavar="SOURCE",
        help="Enable one scoped Host directory source (repeatable)",
    )
    skills_parser.add_argument(
        "--disable-source",
        action="append",
        choices=SKILL_DISCOVERY_SOURCE_IDS,
        default=None,
        metavar="SOURCE",
        help="Disable one scoped Host directory source (repeatable)",
    )
    skills_parser.add_argument("--json", action="store_true", help="Print JSON")

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
    tool_execution_parser.add_argument(
        "--workspace-only",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Restrict built-in file paths and Bash cwd to the Session workspace",
    )
    tool_execution_parser.add_argument(
        "--max-output-chars",
        type=int,
        default=None,
        help="Maximum characters returned by tool output fields",
    )
    tool_execution_parser.add_argument("--json", action="store_true", help="Print JSON")

    queued_inputs_parser = config_subparsers.add_parser(
        "queued-inputs",
        help="Inspect or set queued input consumption policy",
    )
    queued_inputs_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    queued_inputs_parser.add_argument(
        "--consumption-mode",
        type=_canonical_choice({"one-by-one": "one_by_one", "all-at-once": "all_at_once"}),
        choices=("one_by_one", "all_at_once"),
        metavar="{one-by-one,all-at-once}",
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

    agents_parser = config_subparsers.add_parser(
        "agents",
        help="Inspect or set first-release subagent policy",
    )
    agents_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    agents_parser.add_argument("--expected-revision", type=int)
    agents_parser.add_argument(
        "--allow-agent",
        dest="allowed_agent_id_patterns",
        action="append",
        default=None,
        help="Allowed Agent id or glob pattern (repeatable)",
    )
    agents_parser.add_argument(
        "--allow-model",
        dest="allowed_model_patterns",
        action="append",
        default=None,
        help="Allowed provider/endpoint key or glob pattern (repeatable)",
    )
    agents_parser.add_argument(
        "--execution-mode",
        dest="allowed_execution_modes",
        action="append",
        choices=("sync", "async"),
        default=None,
        help="Allowed subagent execution mode (repeatable)",
    )
    for flag, dest, help_text in (
        ("--write", "allow_write", "Allow state-changing tools"),
        ("--network", "allow_network", "Allow tools that require network access"),
        ("--mcp", "allow_mcp", "Allow MCP tools"),
        ("--external-paths", "allow_external_paths", "Allow tools to use external paths"),
    ):
        agents_parser.add_argument(
            flag,
            dest=dest,
            action=argparse.BooleanOptionalAction,
            default=None,
            help=help_text,
        )
    agents_parser.add_argument("--json", action="store_true", help="Print JSON")

    compaction_parser = config_subparsers.add_parser(
        "compaction",
        help="Inspect or set compaction model routing and request parameters",
        description=(
            "Inspect or update the model route and request overrides used for context "
            "compaction.\nOmitted options retain their current values."
        ),
        epilog="""Examples:
  ouro config compaction
  ouro config compaction --provider openai --endpoint gpt-5.6 --thinking-effort low
  ouro config compaction --runtime-endpoint
  ouro config compaction --clear

Summary request settings apply to every selected route.
The output limit defaults to none and does not inherit the endpoint limit.
--runtime-endpoint keeps summary request settings; --clear resets the route and settings.""",
    )
    compaction_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    compaction_parser.add_argument("--provider", dest="provider_id", help="Provider id")
    compaction_parser.add_argument("--endpoint", dest="endpoint_id", help="Endpoint id")
    compaction_parser.add_argument(
        "--default-model", action="store_true", help="Use the system default endpoint"
    )
    compaction_parser.add_argument(
        "--runtime-endpoint",
        action="store_true",
        help="Use each Session's runtime endpoint while keeping summary request settings",
    )
    compaction_parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        metavar="FLOAT",
        help="Summary sampling temperature",
    )
    compaction_parser.add_argument(
        "--max-output-tokens",
        type=_output_limit,
        default=argparse.SUPPRESS,
        metavar="TOKENS|none",
        help="Summary output limit; none means no configured limit",
    )
    compaction_parser.add_argument(
        "--thinking-mode",
        choices=("default", "enabled", "disabled"),
        default=None,
        help="Summary reasoning intent",
    )
    compaction_parser.add_argument(
        "--thinking-effort",
        choices=("minimal", "low", "medium", "high", "xhigh", "max"),
        default=None,
        help="Dedicated-model reasoning effort; implies enabled when mode is omitted",
    )
    compaction_parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear the dedicated endpoint and all compaction model overrides",
    )
    compaction_parser.add_argument("--json", action="store_true", help="Print JSON")
