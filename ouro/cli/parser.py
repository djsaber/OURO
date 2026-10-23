"""Build the CLI parser with explicit command registration order."""

from __future__ import annotations

import argparse
from typing import Any

from ouro import __version__
from ouro.cli.parsers.agents import _add_agent_parser
from ouro.cli.parsers.approvals import _add_approval_parsers
from ouro.cli.parsers.arguments import _add_session_backend_arg, _add_task_backend_arg
from ouro.cli.parsers.configuration import _add_config_parser
from ouro.cli.parsers.mcp import _add_mcp_parser
from ouro.cli.parsers.plugins import _add_plugin_parser
from ouro.cli.parsers.providers import _add_provider_parser
from ouro.cli.parsers.resources import _add_resource_parser
from ouro.cli.parsers.run import _add_run_parser
from ouro.cli.parsers.sessions import _add_session_parser
from ouro.cli.parsers.skills import _add_skills_parser
from ouro.cli.parsers.usage import _add_usage_parser
from ouro.core.approvals import SESSION_APPROVAL_MODES
from ouro.data_commands import add_data_arguments


class OuroArgumentParser(argparse.ArgumentParser):
    """Render command examples predictably while retaining wrapped option help."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        super().__init__(*args, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = OuroArgumentParser(
        prog="ouro",
        description=(
            "Local-first agent runtime for interactive work, one-shot runs, and durable "
            "Session management.\nRun without a command to start the full-screen TUI."
        ),
        epilog="""Getting started:
  ouro
  ouro provider templates
  ouro provider add openai --type openai --api-key-env OPENAI_API_KEY --default
  ouro run "Explain this repository"

Use 'ouro COMMAND --help' to inspect a workflow and its examples.""",
    )
    parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME for the TUI")
    _add_session_backend_arg(parser, dest="session_backend")
    _add_task_backend_arg(parser, dest="task_backend")
    parser.add_argument("--system-prompt", help="Override the default system prompt")
    parser.add_argument("--session", dest="resume_session_id", help="Resume a durable Session")
    tui_policy = parser.add_mutually_exclusive_group()
    tui_policy.add_argument(
        "--approval-mode",
        dest="tui_approval_mode",
        choices=SESSION_APPROVAL_MODES,
        default="inherit",
        help="Initial approval mode for a new TUI Session",
    )
    tui_policy.add_argument(
        "--afk",
        dest="tui_afk",
        action="store_true",
        help="Start new TUI Sessions in unattended AFK mode",
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"%(prog)s {__version__}",
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
    data = subparsers.add_parser(
        "data",
        help="Import or export OURO configuration and data",
        description="Transfer one configuration domain, Session trees, or native skill files.",
        epilog=(
            "Examples:\n"
            "  ouro data export --kind providers --path providers.zip\n"
            "  ouro data export --kind prompts --path prompts.zip\n"
            "  ouro data export --kind sessions --path all-sessions.zip\n"
            "  ouro data import --kind sessions --path all-sessions.zip --id ses_example\n"
            "  ouro data import --kind skills --path skills.zip --replace"
        ),
    )
    add_data_arguments(data)
    data.add_argument("--home", help="Override OURO_HOME")
    _add_session_backend_arg(data)
    data.add_argument("--json", action="store_true", help="Print JSON")

    return parser
