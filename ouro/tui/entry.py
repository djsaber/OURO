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
        from ouro.tui.app import OuroTuiApp

        result = OuroTuiApp(
            home=home,
            system_prompt=system_prompt,
            session_backend=session_backend,
            task_backend=task_backend,
            approval_mode=approval_mode,
            interaction_mode=interaction_mode,
            resume_session_id=resume_session_id,
            theme=theme,
            welcome_packages=welcome_packages,
            startup=feedback,
        ).run()
        return result if isinstance(result, int) else 0
