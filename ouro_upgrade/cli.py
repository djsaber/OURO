"""Command line entry point for explicitly requested offline upgrades."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from ouro.core.errors import OuroError
from ouro_upgrade.engine import upgrade
from ouro_upgrade.errors import UpgradeError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ouro-upgrade",
        description="Upgrade supported data in an existing, stopped OURO home in place.",
    )
    parser.add_argument("--home", required=True)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--check", action="store_true", help="check without changing business data"
    )
    actions.add_argument("--restore", action="store_true", help="restore an unfinished upgrade")
    parser.add_argument("--session-backend", choices=("files", "sqlite"))
    parser.add_argument("--task-backend", choices=("files", "sqlite"))
    args = parser.parse_args(argv)
    try:
        result = upgrade(**vars(args))
    except (UpgradeError, OuroError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
