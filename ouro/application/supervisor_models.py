from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from ouro.application.dto import ApplicationDTO, Diagnostic
from ouro.core.base import new_id, utc_now
from ouro.core.events import EventRecord

SupervisorRunStatus = Literal[
    "idle",
    "queued",
    "running",
    "waiting_user",
    "waiting_approval",
    "completed",
    "aborted",
    "failed",
    "paused",
    "recoverable",
]
ShutdownMode = Literal["wait", "abort"]


class RunHandle(ApplicationDTO):
    handle_id: str = Field(default_factory=lambda: new_id("handle"))
    session_id: str
    submitted_at: datetime = Field(default_factory=utc_now)


class RuntimeStatusSnapshot(ApplicationDTO):
    handle: RunHandle | None = None
    session_id: str
    run_id: str | None = None
    status: SupervisorRunStatus
    updated_at: datetime = Field(default_factory=utc_now)
    diagnostics: tuple[Diagnostic, ...] = ()


class ShutdownPolicy(ApplicationDTO):
    mode: ShutdownMode = "wait"
    timeout_seconds: float = Field(default=10.0, gt=0)


class RuntimeNotification(ApplicationDTO):
    kind: Literal["event", "diagnostic"]
    event: EventRecord | None = None
    diagnostic: Diagnostic | None = None


class SupervisorStartResult(ApplicationDTO):
    recovered: tuple[RuntimeStatusSnapshot, ...] = ()
