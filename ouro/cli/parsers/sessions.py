"""Register sessions command arguments."""

from __future__ import annotations

import argparse

from ouro.cli.parsers.arguments import _add_session_backend_arg, _canonical_choice
from ouro.core.approvals import SESSION_APPROVAL_MODES


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
        type=_canonical_choice(
            {
                "idle": "idle",
                "active": "active",
                "waiting-approval": "waiting_approval",
                "waiting-user": "waiting_user",
                "failed": "failed",
                "paused": "paused",
                "recoverable": "recoverable",
                "completed": "completed",
            }
        ),
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
        metavar="STATUS",
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
        type=_canonical_choice(
            {"updated-desc": "updated_desc", "created-desc": "created_desc", "title": "title"}
        ),
        choices=("updated_desc", "created_desc", "title"),
        default="updated-desc",
        metavar="{updated-desc,created-desc,title}",
    )

    show_parser = session_subparsers.add_parser("show", help="Show session details")
    show_parser.add_argument("session_id", help="Session id")
    show_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(show_parser)
    show_parser.add_argument("--json", action="store_true", help="Print JSON")

    summary_parser = session_subparsers.add_parser(
        "summary", help="Show all saved session compaction summaries"
    )
    summary_parser.add_argument("session_id", help="Session id")
    summary_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(summary_parser)
    summary_parser.add_argument("--json", action="store_true", help="Print JSON")

    context_parser = session_subparsers.add_parser(
        "context",
        help="Show the latest prepared Provider request context",
    )
    context_parser.add_argument("session_id", help="Session id")
    context_parser.add_argument(
        "--content",
        action="store_true",
        help="Include the exact provider-neutral content from that request",
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

    tags_parser = session_subparsers.add_parser(
        "tags",
        help="Replace a session's tags; omit TAG to clear them",
    )
    tags_parser.add_argument("session_id", help="Session id")
    tags_parser.add_argument("tags", nargs="*", metavar="TAG", help="Complete replacement tag set")
    tags_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(tags_parser)
    tags_parser.add_argument("--json", action="store_true", help="Print JSON")

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

    remove_parser = session_subparsers.add_parser("remove", help="Remove a session")
    remove_parser.add_argument("session_id", help="Session id")
    remove_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(remove_parser)
    remove_parser.add_argument("--force", action="store_true", help="Skip confirmation")
    remove_parser.add_argument("--json", action="store_true", help="Print JSON")

    messages_parser = session_subparsers.add_parser("messages", help="Show session messages")
    messages_parser.add_argument("session_id", help="Session id")
    messages_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(messages_parser)
    messages_parser.add_argument("--cursor", type=int, default=0, help="Opaque page cursor")
    messages_parser.add_argument("--limit", type=int, default=100, help="Page size (1-1000)")
    messages_parser.add_argument("--json", action="store_true", help="Print JSON")

    compact_parser = session_subparsers.add_parser(
        "compact",
        help="Summarize and compress older session context",
    )
    compact_parser.add_argument("session_id", help="Session id")
    compact_parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    _add_session_backend_arg(compact_parser)
    compact_parser.add_argument("--json", action="store_true", help="Print JSON")
