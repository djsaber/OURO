"""Shared labels for user-selected files, uploads and skills in the TUI."""

from __future__ import annotations

from collections.abc import Iterable


def reference_label(name: str) -> str:
    return f"@{name}"


def reference_labels(names: Iterable[str]) -> str:
    return " ".join(reference_label(name) for name in names)
