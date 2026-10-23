"""Register agents command arguments."""

from __future__ import annotations

import argparse

from ouro.cli.parsers.arguments import _add_session_backend_arg, _canonical_choice


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
        model_selection = command.add_mutually_exclusive_group()
        model_selection.add_argument(
            "--model", help="Specific provider/endpoint route or 'default'"
        )
        model_selection.add_argument(
            "--session-model", action="store_true", help="Use the parent conversation model"
        )
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
    spawn.add_argument("--resource", dest="resource_ids", action="append", default=[])
    _add_agent_output_args(spawn)

    wait = commands.add_parser("wait", help="Wait for durable subagent results")
    wait.add_argument("delegation_ids", nargs="+")
    _add_agent_output_args(wait)

    send = commands.add_parser("send", help="Steer or follow up a subagent")
    send.add_argument("delegation_id")
    send.add_argument("message")
    send.add_argument("--request-id", required=True)
    send.add_argument(
        "--mode",
        type=_canonical_choice({"steer": "steer", "follow-up": "follow_up"}),
        choices=("steer", "follow_up"),
        default="steer",
        metavar="{steer,follow-up}",
    )
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
