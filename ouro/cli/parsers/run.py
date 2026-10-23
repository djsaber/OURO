"""Register run command arguments."""

from __future__ import annotations

import argparse

from ouro.cli.parsers.arguments import (
    _add_session_backend_arg,
    _add_task_backend_arg,
    _canonical_choice,
)
from ouro.cli.parsers.providers import _add_provider_args
from ouro.core.approvals import SESSION_APPROVAL_MODES


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
    _add_task_backend_arg(run_parser, dest="run_task_backend")
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
        type=_canonical_choice({"follow-up": "follow_up", "steer": "steer"}),
        choices=("follow_up", "steer"),
        default="follow-up",
        metavar="{follow-up,steer}",
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
