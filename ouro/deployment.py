from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast

from ouro.core.errors import ConfigError, PersistenceError
from ouro.local_locks import FileAccessLock

SessionBackend = Literal["files", "sqlite"]
SESSION_BACKENDS: tuple[SessionBackend, ...] = ("files", "sqlite")
SESSION_BACKEND_ENV = "OURO_SESSION_BACKEND"
TaskBackend = Literal["files", "sqlite"]
TASK_BACKENDS: tuple[TaskBackend, ...] = ("files", "sqlite")
TASK_BACKEND_ENV = "OURO_TASK_BACKEND"


def resolve_home(home: str | Path | None = None) -> Path:
    """Resolve the selected local home without opening or initializing its data."""
    configured = home if home is not None else os.environ.get("OURO_HOME")
    root = Path(configured) if configured is not None else Path.home() / ".ouro"
    return root.expanduser().absolute()


def ensure_home_available(home: str | Path | None = None) -> Path:
    """Block runtime initialization while offline data maintenance needs recovery."""
    root = resolve_home(home).resolve()
    if (root / ".upgrade" / "in-progress.json").exists():
        raise PersistenceError(
            "home has unfinished data maintenance; open the TUI recovery menu. "
            "For an unfinished upgrade, stop OURO and run "
            f'ouro-upgrade --home "{root}" --restore'
        )
    return root


@contextmanager
def access_home(home: str | Path | None = None) -> Iterator[Path]:
    """Keep a standard Host's lifetime mutually exclusive with offline maintenance."""
    root = resolve_home(home)
    lock = FileAccessLock(root / ".upgrade" / "lock")
    if not lock.acquire():
        raise PersistenceError("home is undergoing data maintenance; wait for it to finish")
    try:
        yield ensure_home_available(root)
    finally:
        lock.release()


def resolve_session_backend(value: str | None = None) -> SessionBackend:
    """Resolve the local Host's Session backend from CLI override or environment."""

    configured = value if value is not None else os.environ.get(SESSION_BACKEND_ENV, "files")
    normalized = configured.strip().lower()
    if normalized not in SESSION_BACKENDS:
        expected = ", ".join(SESSION_BACKENDS)
        raise ConfigError(
            f"invalid {SESSION_BACKEND_ENV} value {configured!r}; expected one of: {expected}"
        )
    return cast(SessionBackend, normalized)


def resolve_task_backend(value: str | None = None) -> TaskBackend:
    """Resolve task records independently of the canonical Session backend."""

    configured = value if value is not None else os.environ.get(TASK_BACKEND_ENV, "files")
    normalized = configured.strip().lower()
    if normalized not in TASK_BACKENDS:
        expected = ", ".join(TASK_BACKENDS)
        raise ConfigError(
            f"invalid {TASK_BACKEND_ENV} value {configured!r}; expected one of: {expected}"
        )
    return cast(TaskBackend, normalized)
