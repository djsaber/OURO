from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING

from ouro.tui.startup import TerminalStartup

if TYPE_CHECKING:
    import argparse


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        return 130


def _main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    with TerminalStartup(enabled=not arguments) as startup:
        from ouro.cli.errors import error_exit_code
        from ouro.cli.parser import build_parser
        from ouro.core.errors import OuroError
        from ouro.deployment import access_home

        args = build_parser().parse_args(arguments)
        try:
            _validate_host_options(args)
            if args.command is None:
                from ouro.tui.entry import run_tui

                startup.start()
                return run_tui(
                    home=args.home,
                    system_prompt=args.system_prompt,
                    session_backend=args.session_backend,
                    task_backend=args.task_backend,
                    approval_mode="auto" if args.tui_afk else args.tui_approval_mode,
                    interaction_mode="unattended" if args.tui_afk else "interactive",
                    resume_session_id=args.resume_session_id,
                    startup=startup,
                )
            from ouro.cli.dispatch import dispatch

            with access_home(getattr(args, "run_home", None) or args.home):
                return dispatch(args)
        except OuroError as exc:
            startup.close()
            if bool(getattr(args, "json", False) or getattr(args, "json_mode", False)):
                import json

                print(
                    json.dumps(
                        {
                            "status": "failed",
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                        },
                        ensure_ascii=False,
                    )
                )
            else:
                print(f"Error: {exc}", file=sys.stderr)
            return error_exit_code(exc)


def _validate_host_options(args: argparse.Namespace) -> None:
    from ouro.core.errors import ConfigError

    if args.command is not None and (
        args.resume_session_id is not None or args.tui_afk or args.tui_approval_mode != "inherit"
    ):
        raise ConfigError(
            "--session, --approval-mode, and --afk before a command are TUI options; "
            "use 'ouro COMMAND --help' for command-specific options"
        )


if __name__ == "__main__":
    raise SystemExit(main())
