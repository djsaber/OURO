from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|password|authorization|secret)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_URL = re.compile(r"https?://[^\s]+")
_SENSITIVE_KEY = re.compile(r"(?i)(api[_-]?key|token|password|authorization|secret|credential)")


def redact_text(value: str) -> str:
    """Remove common credential shapes before text crosses a trust boundary."""

    redacted = _SENSITIVE_ASSIGNMENT.sub(r"\1\2***", value)
    redacted = _BEARER.sub("Bearer ***", redacted)
    return _URL.sub(lambda match: public_url(match.group(0)), redacted)


def public_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "***"
    if not parsed.scheme or not parsed.hostname:
        return "***"
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


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
