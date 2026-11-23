from __future__ import annotations

import json
import sys

from ouro.core.errors import ConfigError, CredentialError, PersistenceError


def error_exit_code(exc: Exception) -> int:
    if isinstance(exc, CredentialError):
        return 3
    if isinstance(exc, ConfigError):
        return 2
    if isinstance(exc, PersistenceError):
        return 7
    return 1


def report_error(exc: Exception, *, json_mode: bool) -> int:
    if json_mode:
        payload = {"status": "failed", "error": str(exc), "error_type": type(exc).__name__}
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"Error: {exc}", file=sys.stderr)
    return error_exit_code(exc)
