"""Register resources command arguments."""

from __future__ import annotations

import argparse

from ouro.cli.parsers.arguments import _add_session_backend_arg


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
