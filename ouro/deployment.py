from __future__ import annotations

import os
from typing import Literal, cast

from ouro.core.errors import ConfigError

SessionBackend = Literal["files", "sqlite"]
SESSION_BACKENDS: tuple[SessionBackend, ...] = ("files", "sqlite")
SESSION_BACKEND_ENV = "OURO_SESSION_BACKEND"


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
