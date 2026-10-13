from __future__ import annotations

from pathlib import Path

from ouro.core.approvals import SessionApprovalMode
from ouro.core.errors import PersistenceError, UnsupportedDataVersionError
from ouro.core.interactions import SessionInteractionMode
from ouro.deployment import SessionBackend, TaskBackend, access_home, resolve_home
from ouro.tui.terminal import set_terminal_title, tui_terminal_guard
from ouro.tui.theme import TuiTheme
from ouro.tui.welcome.model import WelcomeStyle


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
    welcome_styles: tuple[WelcomeStyle, ...] = (),
) -> int:
    """Run the full-screen Textual Host and return its process exit code."""

    from ouro.tui.app import OuroTuiApp

    root = resolve_home(home)
    attempted_upgrade = False
    with tui_terminal_guard():
        set_terminal_title("OURO")
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
                        welcome_styles=welcome_styles,
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

            recovery = DataRecoveryApp(
                home=root,
                error=str(failure),
                auto_upgrade=isinstance(failure, UnsupportedDataVersionError)
                and not attempted_upgrade,
                session_backend=session_backend,
                task_backend=task_backend,
                theme=recovery_theme,
            ).run()
            if recovery != "upgraded":
                return 1
            attempted_upgrade = True
            # Return to the homepage; the previous selection may no longer exist.
            resume_session_id = None
