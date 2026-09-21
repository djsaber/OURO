"""CLI data arguments and shared CLI/TUI result presentation."""

from __future__ import annotations

import argparse
from pathlib import Path

from ouro.application.data_transfer import (
    DATA_KINDS,
    DataTransferRequest,
    DataTransferResult,
)


def add_data_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", choices=("export", "import"))
    parser.add_argument("--kind", required=True, choices=DATA_KINDS)
    parser.add_argument("--path", required=True, type=Path, help="OURO archive file")
    parser.add_argument(
        "--id",
        action="append",
        default=[],
        dest="identities",
        help="Select Session trees for either action, or native skills for export; repeatable",
    )
    conflicts = parser.add_mutually_exclusive_group()
    conflicts.add_argument(
        "--replace",
        action="store_true",
        help="Replace the export file or existing imported data; Sessions replace complete trees",
    )
    conflicts.add_argument(
        "--skip-existing",
        action="store_false",
        dest="replace",
        help="Skip existing Session trees on import (default); do not replace other data",
    )
    parser.set_defaults(replace=False)


def data_request(args: argparse.Namespace) -> DataTransferRequest:
    return DataTransferRequest(
        action=args.action,
        kind=args.kind,
        path=args.path,
        identities=tuple(args.identities),
        replace=args.replace,
    )


def format_data_result(result: DataTransferResult) -> str:
    action = "Exported" if result.action == "export" else "Imported"
    direction = "to" if result.action == "export" else "from"
    unit = "configuration" if result.kind not in {"skills", "sessions"} else "item(s)"
    text = f"{action} {result.kind}: {result.count} {unit} {direction} {result.path}"
    if result.kind == "sessions" and result.action == "import":
        text += f"\nExisting Sessions: {result.skipped} skipped, {result.replaced} replaced."
    if result.restart_required:
        text += "\nRestart running OURO hosts to load the imported data."
    if result.diagnostics:
        text += (
            f"\nExternal references preserved in {len(result.diagnostics)} Session(s). "
            "History is available; required plugins, tools, and models are checked on continuation."
        )
    return text
