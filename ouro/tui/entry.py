from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING

from ouro.tui.startup import TerminalStartup

if TYPE_CHECKING:
    from pathlib import Path

    from ouro.core.approvals import SessionApprovalMode
    from ouro.core.interactions import SessionInteractionMode
    from ouro.deployment import SessionBackend, TaskBackend
    from ouro.tui.theme import TuiTheme
    from ouro.tui.welcome.model import ThemePackage


def run_tui(
    *,
    home: str | Path | None = None,
    system_prompt: str | None = None,
    session_backend: SessionBackend | None = None,
    task_backend: TaskBackend | None = None,
    approval_mode: SessionApprovalMode = "inherit",
    interaction_mode: SessionInteractionMode = "interactive",
    resume_session_id: str | None = None,
    theme: TuiTheme | None = None,
    welcome_packages: tuple[ThemePackage, ...] = (),
    startup: TerminalStartup | None = None,
) -> int:
    """Run the full-screen Textual Host and return its process exit code."""

    with nullcontext(startup) if startup is not None else TerminalStartup() as feedback:
        from ouro.core.errors import (
            InvalidConfigDataError,
            PersistenceError,
            UnsupportedDataVersionError,
        )
        from ouro.deployment import access_home, resolve_home
        from ouro.tui.app import OuroTuiApp

        root = resolve_home(home)
        attempted_upgrade = False
        while True:
            recovery_theme = theme
            try:
                with access_home(root):
                    app = OuroTuiApp(
                        home=root,
                        system_prompt=system_prompt,
                        session_backend=session_backend,
                        task_backend=task_backend,
                        approval_mode=approval_mode,
                        interaction_mode=interaction_mode,
                        resume_session_id=resume_session_id,
                        theme=theme,
                        welcome_packages=welcome_packages,
                        startup=feedback,
                    )
                    result = app.run()
                    failure = app.startup_data_error
                    if failure is None:
                        return result if isinstance(result, int) else 0
                    recovery_theme = app.tui_theme
            except PersistenceError as exc:
                failure = exc
            # run() has unmounted the Host and closed its services before the
            # independent recovery App can acquire exclusive home access.
            from ouro.tui.recovery import DataRecoveryApp

            feedback.stop()
            recovery = DataRecoveryApp(
                home=root,
                error=str(failure),
                auto_upgrade=isinstance(
                    failure, (UnsupportedDataVersionError, InvalidConfigDataError)
                )
                and not attempted_upgrade,
                session_backend=session_backend,
                task_backend=task_backend,
                theme=recovery_theme,
            ).run()
            if recovery != "upgraded":
                return 1
            attempted_upgrade = True
            feedback.start()
            # Return to the homepage; the previous selection may no longer exist.
            resume_session_id = None
