"""Offline home upgrade orchestration, independent of the OURO application lifecycle."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from ouro.deployment import (
    SessionBackend,
    TaskBackend,
    resolve_session_backend,
    resolve_task_backend,
)
from ouro_upgrade import configuration, databases, files
from ouro_upgrade.errors import UpgradeError, require
from ouro_upgrade.progress import ProgressCallback
from ouro_upgrade.records import loads
from ouro_upgrade.workspace import Workspace


def pending_operation(home: Path) -> Literal["upgrade", "reset"] | None:
    """Identify which recovery action an interrupted maintenance attempt permits."""
    marker = home / ".upgrade" / "in-progress.json"
    if not marker.exists():
        return None
    try:
        return (
            "reset" if loads(marker.read_text("utf-8")).get("operation") == "reset" else "upgrade"
        )
    except OSError, UnicodeError, UpgradeError:
        # Restore owns full validation of the marker and its referenced backup.
        return "upgrade"


def upgrade(
    home: str | Path,
    *,
    check: bool = False,
    restore: bool = False,
    session_backend: SessionBackend | None = None,
    task_backend: TaskBackend | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, object]:
    """Upgrade one stopped home in place; --check may write only .upgrade scratch files."""
    if check and restore:
        raise UpgradeError("check and restore are mutually exclusive")
    # absolute() deliberately does not resolve aliases before Workspace rejects links.
    root = Path(home).expanduser().absolute()
    try:
        with Workspace(root, progress=progress) as work:
            if restore:
                count = work.restore()
                work.progress("complete")
                return {"status": "restored", "files": count}
            sessions = resolve_session_backend(session_backend)
            tasks = resolve_task_backend(task_backend)
            work.start()
            for directory, pattern in (
                (root, ".session-import-*"),
                (root / "trash/sessions", ".*.staging"),
            ):
                require(
                    not files.children(directory, pattern),
                    pattern,
                    "unfinished publication; recover using original OURO version",
                )
            work.progress("configuration")
            configuration.providers(work)
            work.progress("sessions")
            if sessions == "files":
                files.sessions(work)
            else:
                databases.database(work, "sessions.sqlite3", databases.sessions)
            work.progress("tasks")
            if tasks == "files":
                files.tasks(work)
            else:
                databases.database(work, "tasks.sqlite3", databases.tasks)
            work.progress("usage")
            databases.database(work, "billing/usage.sqlite3", databases.usage)
            report: dict[str, object] = {
                "status": "upgrade_required" if work.changes else "current",
                "session_backend": sessions,
                "task_backend": tasks,
                "files": list(work.changes),
                "preserved_outside_scope": [
                    "unselected session/task backends",
                    "trash and maintenance backups",
                    "configuration other than config/providers.json",
                    "credentials, plugins and plugin-private data",
                ],
            }
            if work.changes and not check:
                report.update(
                    status="upgraded",
                    upgrade_id=work.identifier,
                    backup=f".upgrade/{work.identifier}/before",
                    manifest=f".upgrade/{work.identifier}/manifest.json",
                )
                work.publish(report)
            else:
                work.verify_source()
            work.progress("complete")
            return report
    except UpgradeError:
        raise
    except (OSError, UnicodeError, sqlite3.Error) as exc:
        raise UpgradeError(
            f"upgrade I/O failed ({type(exc).__name__}); "
            "if .upgrade/in-progress.json exists, run --restore"
        ) from exc
