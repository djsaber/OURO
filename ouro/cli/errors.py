from __future__ import annotations

from ouro.core.errors import ConfigError, CredentialError, OuroError


def error_exit_code(exc: OuroError) -> int:
    if isinstance(exc, CredentialError):
        return 3
    if isinstance(exc, ConfigError):
        return 2
    return 1
