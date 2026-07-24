from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any

from ouro.config.provider_models import CONFIGURABLE_PROVIDER_TYPES
from ouro.config.reasoning import (
    REASONING_PRESETS,
    EndpointReasoningConfig,
    reasoning_preset,
)
from ouro.core.approvals import SESSION_APPROVAL_MODES
from ouro.deployment import SESSION_BACKENDS


class OuroArgumentParser(argparse.ArgumentParser):
    """Render command examples predictably while retaining wrapped option help."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        super().__init__(*args, **kwargs)


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


def build_parser() -> argparse.ArgumentParser:
    parser = OuroArgumentParser(
        prog="ouro",
        description=(
            "Local-first agent runtime for interactive work, one-shot runs, and durable "
            "Session management.\nRun without a command to start the interactive Host."
        ),
        epilog="""Getting started:
  ouro provider templates
  ouro provider add openai --type openai --api-key-env OPENAI_API_KEY --default
  ouro run "Explain this repository"

Use 'ouro COMMAND --help' to inspect a workflow and its examples.""",
    )
    parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME for interactive mode")
    _add_session_backend_arg(parser, dest="session_backend")
    parser.add_argument("--system-prompt", help="Override the default system prompt")
    interactive_policy = parser.add_mutually_exclusive_group()
    interactive_policy.add_argument(
        "--approval-mode",
        dest="interactive_approval_mode",
        choices=SESSION_APPROVAL_MODES,
        default="inherit",
        help="Initial approval mode for a new interactive session",
    )
    interactive_policy.add_argument(
        "--afk",
        dest="interactive_afk",
        action="store_true",
        help="Start new interactive sessions in unattended AFK mode",
    )
    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version="%(prog)s 0.1.0",
        help="Show version and exit",
    )
    subparsers = parser.add_subparsers(dest="command")

    _add_run_parser(subparsers)
    _add_agent_parser(subparsers)
    _add_session_parser(subparsers)
    _add_resource_parser(subparsers)
    _add_approval_parsers(subparsers)
    _add_config_parser(subparsers)
    _add_provider_parser(subparsers)
    _add_mcp_parser(subparsers)
    _add_plugin_parser(subparsers)
    _add_skills_parser(subparsers)
    _add_usage_parser(subparsers)

    return parser


def _add_agent_parser(subparsers: argparse._SubParsersAction) -> None:
    agent = subparsers.add_parser(
        "agent",
        help="Manage Agent definitions and durable subagents",
    )
    commands = agent.add_subparsers(dest="agent_command", required=True)

    definition = commands.add_parser("definition", help="Manage Agent definitions")
    definition_commands = definition.add_subparsers(
        dest="definition_command",
        required=True,
    )
    for name, help_text in (
        ("list", "List Agent definitions and compatibility"),
        ("tools", "List tools selectable by Agent definitions"),
    ):
        command = definition_commands.add_parser(name, help=help_text)
        _add_agent_output_args(command)

    show = definition_commands.add_parser("show", help="Show one Agent definition")
    show.add_argument("agent_id")
    _add_agent_output_args(show)

    for name in ("create", "update"):
        command = definition_commands.add_parser(name, help=f"{name.title()} an Agent definition")
        command.add_argument("agent_id")
        command.add_argument("--name", required=True)
        command.add_argument("--description", required=True)
        command.add_argument("--system-prompt", required=True)
        command.add_argument("--all-tools", action="store_true")
        command.add_argument("--tool", dest="tool_names", action="append", default=[])
        command.add_argument("--expected-revision", type=int, required=True)
        _add_agent_output_args(command)

    delete = definition_commands.add_parser("delete", help="Delete a user Agent definition")
    delete.add_argument("agent_id")
    delete.add_argument("--expected-revision", type=int, required=True)
    _add_agent_output_args(delete)

    copy = definition_commands.add_parser("copy", help="Copy a builtin Agent definition")
    copy.add_argument("agent_id")
    copy.add_argument("new_agent_id")
    copy.add_argument("--name", required=True)
    copy.add_argument("--expected-revision", type=int, required=True)
    _add_agent_output_args(copy)

    list_agents = commands.add_parser("list", help="List durable subagents")
    list_agents.add_argument("root_session_id")
    list_agents.add_argument("--parent-session-id")
    _add_agent_output_args(list_agents)

    show_agent = commands.add_parser("show", help="Show one subagent result")
    show_agent.add_argument("delegation_id")
    _add_agent_output_args(show_agent)

    spawn = commands.add_parser(
        "spawn",
        help="Run an unattended Host delegation from a completed parent Run",
    )
    spawn.add_argument("parent_session_id")
    spawn.add_argument("parent_run_id")
    spawn.add_argument("agent_id")
    spawn.add_argument("task")
    spawn.add_argument("--description", required=True)
    spawn.add_argument("--request-id", required=True)
    spawn.add_argument("--nickname")
    spawn.add_argument("--resource-id", dest="resource_ids", action="append", default=[])
    _add_agent_output_args(spawn)

    wait = commands.add_parser("wait", help="Wait for durable subagent results")
    wait.add_argument("delegation_ids", nargs="+")
    _add_agent_output_args(wait)

    send = commands.add_parser("send", help="Steer or follow up a subagent")
    send.add_argument("delegation_id")
    send.add_argument("message")
    send.add_argument("--request-id", required=True)
    send.add_argument("--mode", choices=("steer", "follow_up"), default="steer")
    _add_agent_output_args(send)

    interrupt = commands.add_parser("interrupt", help="Interrupt a subagent Run")
    interrupt.add_argument("delegation_id")
    interrupt.add_argument("--reason", default="Interrupted by CLI Host")
    _add_agent_output_args(interrupt)

    close = commands.add_parser("close", help="Close a terminal subagent control handle")
    close.add_argument("delegation_id")
    _add_agent_output_args(close)

    tree = commands.add_parser("tree", help="Show the canonical Agent tree")
    tree.add_argument("root_session_id")
    _add_agent_output_args(tree)

    interactions = commands.add_parser(
        "interactions",
        help="List pending subagent interactions",
    )
    interactions.add_argument("root_session_id")
    _add_agent_output_args(interactions)


def _add_agent_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home")
    _add_session_backend_arg(parser)
    parser.add_argument("--json", action="store_true")


def _add_plugin_parser(subparsers: argparse._SubParsersAction) -> None:
    plugin = subparsers.add_parser("plugin", help="Manage OURO plugins")
    commands = plugin.add_subparsers(dest="plugin_command", required=True)

    scaffold = commands.add_parser("scaffold", help="Create a minimal local user plugin")
    scaffold.add_argument("destination")
    scaffold.add_argument("--plugin-id", required=True)
    scaffold.add_argument("--name", required=True)
    scaffold.add_argument("--namespace")
    _add_plugin_output(scaffold)

    validate = commands.add_parser("validate", help="Validate a plugin package without installing")
    validate.add_argument("source")
    _add_plugin_source_args(validate)
    _add_plugin_output(validate)

    report = commands.add_parser("import-report", help="Inspect portable community resources")
    report.add_argument("source")
    _add_plugin_output(report)

    list_parser = commands.add_parser("list", help="List installed plugins")
    _add_plugin_output(list_parser)
    show = commands.add_parser("show", help="Show plugin provenance and state")
    show.add_argument("plugin_id")
    _add_plugin_output(show)

    list_commands = commands.add_parser("commands", help="List active plugin commands")
    _add_plugin_output(list_commands)
    run_command = commands.add_parser("run-command", help="Run an active plugin command")
    run_command.add_argument("command_id")
    run_command.add_argument("argument", nargs="?", default="")
    run_command.add_argument("--session-id")
    run_command.add_argument("--workspace")
    _add_plugin_output(run_command)

    install = commands.add_parser("install", help="Install immutable plugin bytes")
    install.add_argument("source")
    _add_plugin_source_args(install)
    _add_plugin_mutation_args(install)

    for name in ("authorize", "trust-code"):
        command = commands.add_parser(name, help=f"{name.replace('-', ' ').title()} for a digest")
        command.add_argument("plugin_id")
        command.add_argument("--digest")
        _add_plugin_mutation_args(command)

    revoke = commands.add_parser("revoke", help="Revoke capability or code trust grants")
    revoke.add_argument("plugin_id")
    revoke.add_argument("--code-trust", action="store_true")
    _add_plugin_request_id(revoke)
    _add_plugin_output(revoke)

    configure = commands.add_parser("configure", help="Set validated plugin configuration")
    configure.add_argument("plugin_id")
    configure.add_argument("--values", required=True, help="JSON object")
    configure.add_argument("--credentials", help="JSON slot-to-reference object")
    configure.add_argument("--digest", help="Configure an installed candidate digest")
    _add_plugin_mutation_args(configure)

    select = commands.add_parser("select", help="Select one installed immutable digest")
    select.add_argument("plugin_id")
    select.add_argument("digest")
    select.add_argument("--expected-digest")
    _add_plugin_output(select)

    for name in ("enable", "disable", "restart", "check"):
        command = commands.add_parser(name, help=f"{name.title()} a plugin")
        command.add_argument("plugin_id")
        _add_plugin_output(command)

    update = commands.add_parser("update", help="Install an update side by side")
    update.add_argument("plugin_id")
    update.add_argument("source")
    _add_plugin_source_args(update)
    _add_plugin_request_id(update)
    _add_plugin_output(update)

    reload_parser = commands.add_parser(
        "reload",
        help="Validate, install, and atomically select local development bytes",
    )
    reload_parser.add_argument("plugin_id")
    reload_parser.add_argument("source")
    reload_parser.add_argument(
        "--authorize",
        action="store_true",
        help="Explicitly grant the candidate's declared capabilities",
    )
    reload_parser.add_argument(
        "--trust-code",
        action="store_true",
        help="Explicitly trust the candidate's exact executable digest",
    )
    _add_plugin_source_args(reload_parser)
    _add_plugin_request_id(reload_parser)
    _add_plugin_output(reload_parser)

    rollback = commands.add_parser("rollback", help="Select and activate an installed digest")
    rollback.add_argument("plugin_id")
    rollback.add_argument("digest")
    _add_plugin_output(rollback)

    remove = commands.add_parser("remove", help="Remove an install while retaining data")
    remove.add_argument("plugin_id")
    remove.add_argument("--digest")
    remove.add_argument("--purge-data", action="store_true")
    _add_plugin_request_id(remove)
    _add_plugin_output(remove)

    gc = commands.add_parser("gc", help="Remove unreferenced plugin cache entries")
    _add_plugin_output(gc)


def _add_plugin_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ref")
    parser.add_argument("--subdirectory")


def _add_plugin_mutation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--preview", action="store_true")
    _add_plugin_request_id(parser)
    _add_plugin_output(parser)


def _add_plugin_request_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--request-id",
        help="Audit correlation id; it does not deduplicate repeated requests",
    )


def _add_plugin_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    parser.add_argument("--json", action="store_true")


def _add_usage_parser(subparsers: argparse._SubParsersAction) -> None:
    usage_parser = subparsers.add_parser(
        "usage",
        help="Inspect and manage global provider-reported token usage",
        description=(
            "Query the global provider usage ledger. Values come from provider responses; "
            "OURO context-size estimates are never recorded here."
        ),
    )
    commands = usage_parser.add_subparsers(dest="usage_command", required=True)

    summary = commands.add_parser("summary", help="Summarize matching usage")
    _add_usage_filter_args(summary)
    summary.add_argument(
        "--group-by",
        choices=("date", "provider", "endpoint", "model", "purpose"),
    )
    _add_usage_output_args(summary)

    list_parser = commands.add_parser("list", help="List provider request usage entries")
    _add_usage_filter_args(list_parser)
    list_parser.add_argument("--limit", type=int, default=100)
    _add_usage_output_args(list_parser)

    show = commands.add_parser("show", help="Show one usage entry")
    show.add_argument("entry_id")
    _add_usage_output_args(show)

    modify = commands.add_parser("modify", help="Append an audited correction to one entry")
    modify.add_argument("entry_id")
    modify.add_argument("--input-tokens", type=int)
    modify.add_argument("--output-tokens", type=int)
    modify.add_argument("--cache-read-tokens", type=int)
    modify.add_argument("--cache-write-tokens", type=int)
    cost = modify.add_mutually_exclusive_group()
    cost.add_argument("--estimated-cost", type=float)
    cost.add_argument("--clear-estimated-cost", action="store_true")
    modify.add_argument("--note", required=True, help="Reason retained in the audit log")
    _add_usage_output_args(modify)

    reset = commands.add_parser(
        "reset",
        help="Reset matching entries while retaining audit history",
    )
    _add_usage_filter_args(reset)
    reset.add_argument("--force", action="store_true", help="Skip confirmation")
    reset.add_argument("--note", required=True, help="Reason retained in the audit log")
    _add_usage_output_args(reset)

    audit = commands.add_parser("audit", help="Show recent ledger mutations")
    audit.add_argument("--limit", type=int, default=100)
    _add_usage_output_args(audit)


def _add_usage_filter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--since",
        help="Inclusive ISO date/time; a bare date starts at UTC midnight",
    )
    parser.add_argument("--until", help="Inclusive ISO date or exclusive ISO date/time")
    parser.add_argument("--provider", dest="provider_ids", action="append")
    parser.add_argument("--endpoint", dest="endpoint_ids", action="append")
    parser.add_argument("--model", dest="model_ids", action="append")
    parser.add_argument("--session", dest="session_ids", action="append")
    parser.add_argument("--run", dest="run_ids", action="append")
    parser.add_argument(
        "--purpose",
        dest="purposes",
        action="append",
        choices=("agent", "compaction"),
    )


def _add_usage_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    parser.add_argument("--json", action="store_true", help="Print JSON")


def _add_resource_parser(subparsers: argparse._SubParsersAction) -> None:
    resource_parser = subparsers.add_parser("resource", help="Manage Session resources")
    resource_subparsers = resource_parser.add_subparsers(
        dest="resource_command",
        required=True,
    )

    import_parser = resource_subparsers.add_parser("import", help="Import a local file")
    import_parser.add_argument("session_id", help="Session id")
    import_parser.add_argument("path", help="Source file")
    import_parser.add_argument("--media-type", help="Override detected media type")
    import_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(import_parser)
    import_parser.add_argument("--json", action="store_true", help="Print JSON")

    list_parser = resource_subparsers.add_parser("list", help="List Session resources")
    list_parser.add_argument("session_id", help="Session id")
    list_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(list_parser)
    list_parser.add_argument("--json", action="store_true", help="Print JSON")

    show_parser = resource_subparsers.add_parser("show", help="Show resource metadata")
    show_parser.add_argument("session_id", help="Session id")
    show_parser.add_argument("resource_id", help="Resource id")
    show_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(show_parser)
    show_parser.add_argument("--json", action="store_true", help="Print JSON")

    export_parser = resource_subparsers.add_parser("export", help="Export resource content")
    export_parser.add_argument("session_id", help="Session id")
    export_parser.add_argument("resource_id", help="Resource id")
    export_parser.add_argument("output", help="Destination file")
    export_parser.add_argument("--force", action="store_true", help="Replace destination")
    export_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(export_parser)
    export_parser.add_argument("--json", action="store_true", help="Print JSON")

    remove_parser = resource_subparsers.add_parser("remove", help="Remove a Session reference")
    remove_parser.add_argument("session_id", help="Session id")
    remove_parser.add_argument("resource_id", help="Resource id")
    remove_parser.add_argument("--force", action="store_true", help="Skip confirmation")
    remove_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(remove_parser)
    remove_parser.add_argument("--json", action="store_true", help="Print JSON")


def _add_run_parser(subparsers: argparse._SubParsersAction) -> None:
    run_parser = subparsers.add_parser(
        "run",
        help="Run a single prompt",
        description=(
            "Run one prompt against a configured model route.\n"
            "The command creates a Session unless --session selects an existing one.\n"
            "Output defaults to final text on stdout."
        ),
        epilog="""Examples:
  ouro run "Summarize this project"
  ouro run --provider openai --endpoint gpt-5.6 "Review the architecture"
  ouro run --provider custom --endpoint reasoning --thinking-mode enabled \\
    --thinking-effort high "Solve this carefully"

Reasoning mode and effort are canonical OURO intents. The selected Endpoint must support
them exactly; unsupported requests fail before an HTTP request is sent.""",
    )
    mode = run_parser.add_mutually_exclusive_group()
    mode.add_argument("--print", dest="print_mode", action="store_true", help="Print final text")
    mode.add_argument("--json", dest="json_mode", action="store_true", help="Print JSON result")
    run_parser.add_argument("--home", dest="run_home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(run_parser, dest="run_session_backend")
    run_parser.add_argument("--session", dest="session_id", help="Continue an existing session")
    run_parser.add_argument(
        "--resource",
        dest="resource_ids",
        action="append",
        default=[],
        metavar="RESOURCE_ID",
        help="Attach an imported Session resource to this input (repeatable)",
    )
    run_parser.add_argument("--title", help="Title for a new session")
    run_policy = run_parser.add_mutually_exclusive_group()
    run_policy.add_argument(
        "--approval-mode",
        choices=SESSION_APPROVAL_MODES,
        help="Set the session approval mode before running",
    )
    run_policy.add_argument(
        "--afk",
        action="store_true",
        help="Apply the unattended AFK policy preset before running",
    )
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
    run_parser.add_argument(
        "prompt",
        nargs="+",
        metavar="PROMPT",
        help="Prompt text; multiple shell words are joined with spaces",
    )


def _add_session_parser(subparsers: argparse._SubParsersAction) -> None:
    session_parser = subparsers.add_parser("session", help="Manage sessions")
    session_subparsers = session_parser.add_subparsers(
        dest="session_command",
        required=True,
    )

    list_parser = session_subparsers.add_parser("list", help="List sessions")
    list_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(list_parser)
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
    _add_session_backend_arg(show_parser)
    show_parser.add_argument("--json", action="store_true", help="Print JSON")

    context_parser = session_subparsers.add_parser(
        "context",
        help="Show the current unified Provider context status or content",
    )
    context_parser.add_argument("session_id", help="Session id")
    context_parser.add_argument(
        "--content",
        action="store_true",
        help="Include the current provider-neutral content projected for RuntimeAdapter",
    )
    context_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(context_parser)
    context_parser.add_argument("--json", action="store_true", help="Print JSON")

    rename_parser = session_subparsers.add_parser("rename", help="Rename a session")
    rename_parser.add_argument("session_id", help="Session id")
    rename_parser.add_argument("title", help="New title")
    rename_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(rename_parser)
    rename_parser.add_argument("--json", action="store_true", help="Print JSON")

    approval_mode_parser = session_subparsers.add_parser(
        "approval-mode",
        help="Set the approval mode for a session",
    )
    approval_mode_parser.add_argument("session_id", help="Session id")
    approval_mode_parser.add_argument(
        "mode",
        choices=SESSION_APPROVAL_MODES,
        help="Session approval mode",
    )
    approval_mode_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(approval_mode_parser)
    approval_mode_parser.add_argument("--json", action="store_true", help="Print JSON")

    afk_parser = session_subparsers.add_parser(
        "afk",
        help="Enable or disable AFK mode for a session",
    )
    afk_parser.add_argument("session_id", help="Session id")
    afk_parser.add_argument("state", choices=["on", "off"], help="AFK mode state")
    afk_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(afk_parser)
    afk_parser.add_argument("--json", action="store_true", help="Print JSON")

    for command, help_text in (
        ("archive", "Archive a session"),
        ("unarchive", "Restore an archived session to the catalog"),
        ("pin", "Pin a session"),
        ("unpin", "Unpin a session"),
    ):
        state_parser = session_subparsers.add_parser(command, help=help_text)
        state_parser.add_argument("session_id", help="Session id")
        state_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
        _add_session_backend_arg(state_parser)
        state_parser.add_argument("--json", action="store_true", help="Print JSON")

    branch_parser = session_subparsers.add_parser("branch", help="Branch at a message boundary")
    branch_parser.add_argument("session_id", help="Source session id")
    branch_parser.add_argument("message_id", help="Canonical source message id")
    branch_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(branch_parser)
    branch_parser.add_argument("--json", action="store_true", help="Print JSON")

    replay_parser = session_subparsers.add_parser("replay", help="Project persisted events")
    replay_parser.add_argument("session_id", help="Session id")
    replay_parser.add_argument("--run", dest="run_id", help="Filter by run id")
    replay_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(replay_parser)
    replay_parser.add_argument("--json", action="store_true", help="Print JSON")

    remove_parser = session_subparsers.add_parser("remove", help="Remove a session")
    remove_parser.add_argument("session_id", help="Session id")
    remove_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(remove_parser)
    remove_parser.add_argument("--force", action="store_true", help="Skip confirmation")
    remove_parser.add_argument("--json", action="store_true", help="Print JSON")

    export_parser = session_subparsers.add_parser("export", help="Export session data")
    export_parser.add_argument("session_id", help="Session id")
    export_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(export_parser)
    export_parser.add_argument(
        "--output",
        help="Write export to a file instead of stdout",
    )
    export_parser.add_argument("--json", action="store_true", help="Print JSON")

    messages_parser = session_subparsers.add_parser("messages", help="Show session messages")
    messages_parser.add_argument("session_id", help="Session id")
    messages_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(messages_parser)
    messages_parser.add_argument("--cursor", type=int, default=0, help="Opaque page cursor")
    messages_parser.add_argument("--limit", type=int, default=100, help="Page size (1-1000)")
    messages_parser.add_argument("--json", action="store_true", help="Print JSON")

    events_parser = session_subparsers.add_parser("events", help="Show session events")
    events_parser.add_argument("session_id", help="Session id")
    events_parser.add_argument("--run", dest="run_id", help="Filter by run id")
    events_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(events_parser)
    events_parser.add_argument("--cursor", type=int, default=0, help="Opaque page cursor")
    events_parser.add_argument("--limit", type=int, default=100, help="Page size (1-1000)")
    events_parser.add_argument("--json", action="store_true", help="Print JSON")

    compact_parser = session_subparsers.add_parser(
        "compact",
        help="Summarize and compress older session context",
    )
    compact_parser.add_argument("session_id", help="Session id")
    compact_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(compact_parser)
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
    _add_session_backend_arg(approve_parser)
    approve_parser.add_argument("--json", action="store_true", help="Print JSON")

    reject_parser = subparsers.add_parser(
        "reject",
        help="Reject a pending tool call for a session",
    )
    reject_parser.add_argument("session_id", help="Session id")
    reject_parser.add_argument("--run", dest="run_id", help="Run id (default: active run)")
    reject_parser.add_argument("--reason", default="User rejected", help="Rejection reason")
    reject_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(reject_parser)
    reject_parser.add_argument("--json", action="store_true", help="Print JSON")

    abort_parser = subparsers.add_parser(
        "abort",
        help="Abort the active run for a session",
    )
    abort_parser.add_argument("session_id", help="Session id")
    abort_parser.add_argument("--run", dest="run_id", help="Run id (default: active run)")
    abort_parser.add_argument("--reason", default="User aborted", help="Abort reason")
    abort_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(abort_parser)
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
        choices=["auto", "approval", "deny"],
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

    agents_parser = config_subparsers.add_parser(
        "agents",
        help="Inspect or set first-release subagent policy",
    )
    agents_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    agents_parser.add_argument("--expected-revision", type=int)
    agents_parser.add_argument("--max-depth", type=int)
    agents_parser.add_argument(
        "--allow-agent",
        dest="allowed_agent_id_patterns",
        action="append",
        default=None,
        help="Allowed Agent id or glob pattern (repeatable)",
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

--runtime-endpoint only clears the dedicated model route. --clear also removes temperature,
token, and reasoning overrides.""",
    )
    compaction_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    compaction_parser.add_argument("--provider", dest="provider_id", help="Provider id")
    compaction_parser.add_argument("--endpoint", dest="endpoint_id", help="Endpoint id")
    compaction_parser.add_argument(
        "--runtime-endpoint",
        action="store_true",
        help="Clear the dedicated endpoint and use the session runtime endpoint",
    )
    compaction_parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        metavar="FLOAT",
        help="Sampling temperature override",
    )
    compaction_parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        metavar="INT",
        help="Maximum output token override",
    )
    compaction_parser.add_argument(
        "--thinking-mode",
        choices=("default", "enabled", "disabled"),
        default=None,
        help="Reasoning intent; default uses the Endpoint's configured default",
    )
    compaction_parser.add_argument(
        "--thinking-effort",
        choices=("minimal", "low", "medium", "high", "xhigh", "max"),
        default=None,
        help="Exact reasoning effort; implies enabled when mode is omitted",
    )
    compaction_parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear the dedicated endpoint and all compaction model overrides",
    )
    compaction_parser.add_argument("--json", action="store_true", help="Print JSON")


def _add_provider_parser(subparsers: argparse._SubParsersAction) -> None:
    provider_parser = subparsers.add_parser(
        "provider",
        help="Manage providers and model endpoints",
        description=(
            "Manage Provider identities, credentials, connection defaults, and model "
            "Endpoints.\nIntegrated types install reviewed Endpoint templates; compatible "
            "types start empty."
        ),
        epilog="""Typical workflow:
  ouro provider templates
  ouro provider add openai --type openai --api-key-env OPENAI_API_KEY --default
  ouro provider endpoint list openai
  ouro provider check openai

For a private or gateway model, add an openai-compatible or anthropic-compatible Provider,
then create its model route with 'ouro provider endpoint set'.""",
    )
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

    provider_templates_parser = provider_subparsers.add_parser(
        "templates",
        help="List or show built-in provider templates",
        description=(
            "List reviewed built-in Provider templates, or show the complete Endpoint "
            "catalog for one template."
        ),
    )
    provider_templates_parser.add_argument("template_id", nargs="?", help="Template id")
    provider_templates_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    provider_templates_parser.add_argument("--json", action="store_true", help="Print JSON")

    provider_add_parser = provider_subparsers.add_parser(
        "add",
        help="Add a provider; integrated types include preset endpoints",
        description=(
            "Create a new Provider. Integrated types install their reviewed Endpoint "
            "catalog.\nCompatible types create an empty Provider for custom Endpoints."
        ),
        epilog="""Examples:
  ouro provider add openai --type openai --api-key-env OPENAI_API_KEY --default
  ouro provider add private --type openai-compatible \\
    --base-url https://models.example.com/v1 --api-key-env PRIVATE_API_KEY

Provider ids are unique. Use 'provider update' for an existing Provider or 'provider replace'
when its type and complete template-owned configuration must change.""",
    )
    provider_add_parser.add_argument("provider_id", help="Provider/supplier id")
    _add_provider_identity_args(provider_add_parser)
    _add_provider_mutation_args(
        provider_add_parser,
        enabled_default=True,
    )

    provider_update_parser = provider_subparsers.add_parser(
        "update",
        help="Update a provider and refresh integrated preset endpoints",
        description=(
            "Update connection, credential, retry, or enabled state without changing the "
            "Provider type.\nIntegrated types refresh template-owned Endpoints and preserve "
            "user-created Endpoints."
        ),
    )
    provider_update_parser.add_argument("provider_id", help="Provider/supplier id")
    _add_provider_mutation_args(
        provider_update_parser,
        enabled_default=None,
    )

    provider_replace_parser = provider_subparsers.add_parser(
        "replace",
        help="Replace a provider; integrated types include preset endpoints",
        description=(
            "Replace an existing Provider with the selected type and its complete initial "
            "configuration.\nUse this when changing Provider type or intentionally resetting "
            "template-owned Endpoints."
        ),
    )
    provider_replace_parser.add_argument("provider_id", help="Provider/supplier id")
    _add_provider_identity_args(provider_replace_parser)
    _add_provider_mutation_args(
        provider_replace_parser,
        enabled_default=True,
    )

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
        description=(
            "Send a minimal request through a configured Endpoint.\n"
            "Runtime flags override configuration for this check only and are not persisted."
        ),
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
        description=(
            "Manage stable model routes and their capabilities.\nReasoning support belongs "
            "to the Endpoint because models behind the same Provider can differ."
        ),
        epilog="""Examples:
  ouro provider endpoint list openai
  ouro provider endpoint show openai gpt-5.6
  ouro provider endpoint set private chat --model private-model --default

Use 'ouro provider endpoint set --help' for custom capability and reasoning contracts.""",
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
        description=(
            "Create or update one model Endpoint.\nOmitted fields retain their stored values "
            "when updating; reasoning preset and full JSON options are mutually exclusive."
        ),
        epilog=f"""Examples:
  ouro provider endpoint set private chat --model private-model --default
  ouro provider endpoint set private reasoning --model reasoning-model \\
    --reasoning-preset reasoning-content-effort --default
  ouro provider endpoint set claude custom --model custom-claude \\
    --reasoning-preset anthropic-adaptive-effort

Reasoning presets:
  {", ".join(REASONING_PRESETS)}

Presets are explicit declarations, not capability detection. Use --reasoning-config when a
model supports different native fields or only a subset of efforts. Its JSON shape is:
  {{"capability": {{...}}, "profile": {{"name": "...", "variants": {{...}}}}}}

Reasoning-owned fields must not also appear in --request-options. Invalid or incomplete
contracts are rejected before configuration is written.""",
    )
    endpoint_set_parser.add_argument("provider_id", help="Provider/supplier id")
    endpoint_set_parser.add_argument("endpoint_id", help="Stable endpoint id")
    endpoint_set_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")

    route_group = endpoint_set_parser.add_argument_group("model route")
    route_group.add_argument("--model", metavar="MODEL_ID", help="Provider-native model id")
    route_group.add_argument(
        "--base-url",
        metavar="URL",
        help="Endpoint URL override; otherwise inherit the Provider base URL",
    )
    route_group.add_argument(
        "--api-path",
        metavar="PATH",
        help="API path override; otherwise inherit Provider or protocol default",
    )
    route_group.add_argument(
        "--temperature",
        type=float,
        default=None,
        metavar="FLOAT",
        help="Sampling temperature (0-2)",
    )
    route_group.add_argument(
        "--context-window",
        type=int,
        default=None,
        metavar="TOKENS",
        help="Total model context window",
    )
    route_group.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        metavar="TOKENS",
        help="Maximum output tokens; must not exceed context window",
    )

    capability_group = endpoint_set_parser.add_argument_group("endpoint capabilities")
    _add_endpoint_capability_args(capability_group)

    reasoning_group = endpoint_set_parser.add_argument_group("reasoning contract")
    endpoint_reasoning_group = reasoning_group.add_mutually_exclusive_group()
    endpoint_reasoning_group.add_argument(
        "--reasoning-preset",
        type=_reasoning_preset,
        metavar="PRESET",
        help="Install a common native reasoning mapping; see preset list below",
    )
    endpoint_reasoning_group.add_argument(
        "--reasoning-config",
        type=_endpoint_reasoning_config,
        metavar="JSON",
        help="Complete capability and native profile mapping as one JSON object",
    )
    reasoning_group.add_argument(
        "--request-options",
        type=_json_object,
        metavar="JSON",
        help="Additional provider request fields not owned by the reasoning profile",
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


def _add_provider_identity_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--type",
        dest="provider_type",
        choices=CONFIGURABLE_PROVIDER_TYPES,
        required=True,
        help=(
            "Provider product type; integrated types install templates, compatible types "
            "start without Endpoints"
        ),
    )


def _add_provider_mutation_args(
    parser: argparse.ArgumentParser,
    *,
    enabled_default: bool | None,
) -> None:
    parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    parser.add_argument(
        "--endpoint-id",
        help="Override the default preset endpoint id (integrated types only)",
    )
    parser.add_argument(
        "--model",
        help="Override the default preset endpoint model id (integrated types only)",
    )
    parser.add_argument(
        "--base-url",
        help="Provider base URL used as the default for endpoints",
    )
    parser.add_argument(
        "--api-path",
        help="Provider API path used as the default for endpoints",
    )
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument(
        "--api-key",
        help="Inline API key (stored as plain text in local config; prefer --api-key-env)",
    )
    credentials.add_argument(
        "--api-key-env",
        metavar="ENV",
        help="Environment variable containing the API key",
    )
    parser.add_argument("--timeout", type=float, default=None, help="Timeout seconds")
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=None,
        help="Maximum provider request attempts (including the first request)",
    )
    parser.add_argument(
        "--retry-initial-backoff",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Delay before the first retry",
    )
    parser.add_argument(
        "--retry-max-backoff",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Maximum delay between retries",
    )
    parser.add_argument(
        "--retry-backoff-multiplier",
        type=float,
        default=None,
        help="Exponential retry backoff multiplier",
    )
    parser.add_argument(
        "--enabled",
        action=argparse.BooleanOptionalAction,
        default=enabled_default,
        help="Enable or disable provider",
    )
    parser.add_argument(
        "--default",
        action="store_true",
        help="Set provider as default when it has a default endpoint",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON")


def _add_mcp_parser(subparsers: argparse._SubParsersAction) -> None:
    mcp_parser = subparsers.add_parser("mcp", help="Manage MCP servers")
    mcp_subparsers = mcp_parser.add_subparsers(dest="mcp_command", required=True)

    list_parser = mcp_subparsers.add_parser("list", help="List MCP servers")
    _add_mcp_common_output_args(list_parser)

    show_parser = mcp_subparsers.add_parser("show", help="Show an MCP server")
    show_parser.add_argument("server_id", help="MCP server id")
    _add_mcp_common_output_args(show_parser)

    set_parser = mcp_subparsers.add_parser("set", help="Create or update an MCP server")
    set_parser.add_argument("server_id", help="MCP server id")
    set_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    set_parser.add_argument("--name", help="Display name (defaults to server id)")
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
    remove_parser.add_argument("server_id", help="MCP server id")
    _add_mcp_common_output_args(remove_parser)

    check_parser = mcp_subparsers.add_parser("check", help="Check an MCP server connection")
    check_parser.add_argument("server_id", help="MCP server id")
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
    route_group = parser.add_argument_group("model route overrides")
    if include_provider:
        route_group.add_argument(
            "--provider",
            dest=f"{prefix}provider_id",
            default=None,
            metavar="PROVIDER_ID",
            help="Configured provider id",
        )
    route_group.add_argument(
        "--endpoint",
        dest=f"{prefix}endpoint_id",
        metavar="ENDPOINT_ID",
        help="Configured endpoint id",
    )
    route_group.add_argument(
        "--base-url",
        dest=f"{prefix}base_url",
        metavar="URL",
        help="Provider base URL",
    )
    route_group.add_argument(
        "--api-path",
        dest=f"{prefix}api_path",
        metavar="PATH",
        help="Provider API path",
    )
    route_group.add_argument(
        "--api-key",
        dest=f"{prefix}api_key",
        metavar="KEY",
        help="Inline API key for this command (prefer --api-key-env)",
    )
    route_group.add_argument(
        "--api-key-env",
        dest=f"{prefix}api_key_env",
        metavar="ENV",
        help="API key env var",
    )
    route_group.add_argument(
        "--timeout",
        dest=f"{prefix}timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Provider timeout seconds",
    )
    route_group.add_argument(
        "--temperature",
        dest=f"{prefix}temperature",
        type=float,
        default=None,
        metavar="FLOAT",
        help="Sampling temperature",
    )
    route_group.add_argument(
        "--context-window",
        dest=f"{prefix}context_window",
        type=int,
        default=None,
        metavar="INT",
        help="Endpoint context window",
    )
    route_group.add_argument(
        "--max-output-tokens",
        dest=f"{prefix}max_output_tokens",
        type=int,
        default=None,
        metavar="INT",
        help="Endpoint maximum output tokens",
    )
    capability_group = parser.add_argument_group("endpoint capability overrides")
    _add_endpoint_capability_args(capability_group, prefix=prefix)

    reasoning_group = parser.add_argument_group("reasoning intent")
    reasoning_group.add_argument(
        "--thinking-mode",
        dest=f"{prefix}thinking_mode",
        choices=("default", "enabled", "disabled"),
        default=None,
        help="Reasoning intent; default uses the Endpoint's configured default",
    )
    reasoning_group.add_argument(
        "--thinking-effort",
        dest=f"{prefix}thinking_effort",
        choices=_THINKING_EFFORTS,
        default=None,
        help="Exact reasoning effort; implies enabled when mode is omitted",
    )


_THINKING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")


def _add_endpoint_capability_args(
    parser: argparse._ActionsContainer,
    *,
    prefix: str = "",
) -> None:
    for options, dest, help_text in (
        (("tools",), "supports_tools", "tool calling"),
        (("vision",), "supports_vision", "image/multimodal input"),
        (("streaming",), "supports_streaming", "streaming output"),
        (("structured-output",), "supports_structured_output", "structured output"),
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


def _endpoint_reasoning_config(value: str) -> EndpointReasoningConfig:
    try:
        return EndpointReasoningConfig.model_validate(_json_object(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid reasoning config: {exc}") from exc


def _reasoning_preset(value: str) -> EndpointReasoningConfig:
    try:
        return reasoning_preset(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


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
