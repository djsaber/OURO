from __future__ import annotations

from ouro.core.errors import ConfigError, CredentialError, OuroError, PersistenceError


def error_exit_code(exc: OuroError) -> int:
    if isinstance(exc, CredentialError):
        return 3
    if isinstance(exc, ConfigError):
        return 2
    if isinstance(exc, PersistenceError):
        return 7
    return 1
