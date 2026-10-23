"""Register usage command arguments."""

from __future__ import annotations

import argparse


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
        choices=(
            "date",
            "provider",
            "endpoint",
            "model",
            "workspace",
            "session",
            "run",
            "plugin",
            "purpose",
            "billing_mode",
        ),
        action="append",
        help="Repeat to group by several dimensions",
    )
    _add_usage_output_args(summary)

    list_parser = commands.add_parser("list", help="List provider request usage entries")
    _add_usage_filter_args(list_parser)
    list_parser.add_argument("--limit", type=int, default=100)
    list_parser.add_argument("--before", help="JSON next_cursor returned by usage list")
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
    audit.add_argument("--before", type=int, help="next_cursor returned by usage audit")
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
    parser.add_argument("--plugin", dest="plugin_ids", action="append")
    parser.add_argument("--workspace", dest="workspaces", action="append")
    parser.add_argument(
        "--billing-mode",
        dest="billing_modes",
        action="append",
        choices=("unknown", "usage_based", "subscription"),
    )
    parser.add_argument(
        "--purpose",
        dest="purposes",
        action="append",
        choices=("agent", "compaction", "plugin"),
    )


def _add_usage_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", metavar="HOME", help="Override OURO_HOME")
    parser.add_argument("--json", action="store_true", help="Print JSON")
