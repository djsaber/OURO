from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ScheduledInvocation:
    """Host-assigned occurrence identity for business deduplication and diagnostics."""

    task_id: str
    execution_id: str
    scheduled_at: datetime
