"""Coarse maintenance progress, independent of any presentation or event loop."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

UpgradePhase = Literal[
    "sessions",
    "tasks",
    "usage",
    "verify_source",
    "backup",
    "publish",
    "verify",
    "restore",
    "reset",
    "complete",
]


@dataclass(frozen=True)
class UpgradeProgress:
    phase: UpgradePhase
    completed: int = 0
    total: int | None = None


type ProgressCallback = Callable[[UpgradeProgress], None]
