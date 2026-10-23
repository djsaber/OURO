"""Register approvals command arguments."""

from __future__ import annotations

import argparse

from ouro.cli.parsers.arguments import _add_session_backend_arg, _json_object


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
