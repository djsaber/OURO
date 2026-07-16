from __future__ import annotations

import re
from pathlib import PurePosixPath

_WINDOWS_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z", re.I)


def portable_relative_path(
    value: str,
    *,
    allow_backslash: bool = False,
    allow_trailing_slash: bool = False,
) -> PurePosixPath:
    """Parse a relative path whose meaning is stable on supported filesystems."""

    if "\\" in value and not allow_backslash:
        raise ValueError("path must use '/' separators")
    normalized = value.replace("\\", "/") if allow_backslash else value
    if allow_trailing_slash and normalized.endswith("/") and not normalized.endswith("//"):
        normalized = normalized[:-1]
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ValueError("path must be normalized and relative")
    relative = PurePosixPath(normalized)
    if (
        not relative.parts
        or relative.is_absolute()
    ):
        raise ValueError("path must be normalized and relative")
    for part in relative.parts:
        if (
            ":" in part
            or part.endswith((" ", "."))
            or _WINDOWS_DEVICE.fullmatch(part) is not None
            or any(ord(character) < 32 for character in part)
        ):
            raise ValueError(f"path component is not portable: {part}")
    return relative
