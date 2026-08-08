from __future__ import annotations

import re
from collections.abc import Iterable
from threading import Lock
from urllib.parse import urlsplit, urlunsplit

_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|password|authorization|secret)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_URL = re.compile(r"https?://[^\s]+")
_SENSITIVE_KEY = re.compile(r"(?i)(api[_-]?key|token|password|authorization|secret|credential)")


class SensitiveValueRedactor:
    """Thread-safe exact-value redaction learned at credential resolution time."""

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._values = {value for value in values if value}
        self._lock = Lock()

    def add(self, value: str) -> None:
        if not value:
            return
        with self._lock:
            self._values.add(value)

    def redact(self, value: str) -> str:
        with self._lock:
            sensitive = sorted(self._values, key=len, reverse=True)
        redacted = value
        for secret in sensitive:
            redacted = redacted.replace(secret, "[REDACTED]")
        return redact_text(redacted)


def redact_text(value: str) -> str:
    """Remove common credential shapes before text crosses a trust boundary."""

    redacted = _SENSITIVE_ASSIGNMENT.sub(r"\1\2***", value)
    redacted = _BEARER.sub("Bearer ***", redacted)
    return _URL.sub(lambda match: public_url(match.group(0)), redacted)


def public_url(value: str) -> str:
    """Return a credential-free URL projection for arbitrary untrusted text."""

    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.hostname:
            return "***"
        host = parsed.hostname
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        # urllib validates some authority fields lazily (notably ``port``), so
        # parsing alone does not establish that a URL is safe to project.
        return "***"


def redact_data(value: object, *, key: str | None = None) -> object:
    if key is not None and _SENSITIVE_KEY.search(key):
        return "***"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            str(item_key): redact_data(item, key=str(item_key)) for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item) for item in value)
    return value
