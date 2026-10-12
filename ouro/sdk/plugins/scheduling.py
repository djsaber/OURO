"""Owned persistent schedules, independent of invocation-local background tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Literal, Protocol

from .models import ModelReasoning
from .state import JsonValue


@dataclass(frozen=True)
class TaskSchedule:
    frequency: Literal["hourly", "daily", "weekdays", "weekly", "monthly", "custom"]
    starts_at: datetime
    timezone: str = "UTC"
    ends_at: datetime | None = None
    at: time = time(9)
    interval: int = 1
    unit: Literal["minutes", "hours", "days", "weeks", "months"] = "days"
    weekdays: tuple[int, ...] = ()
    month_day: int | None = None


@dataclass(frozen=True)
class ScheduledAgent:
    prompt: str
    workspace: str
    model: str = "default"
    approval_mode: Literal["afk", "inherit", "auto", "approval", "deny"] = "afk"
    reasoning: ModelReasoning = field(default_factory=ModelReasoning)
    session_mode: Literal["new", "reuse"] = "new"
    kind: Literal["agent"] = field(default="agent", init=False)


@dataclass(frozen=True)
class ScheduledService:
    plugin_id: str
    service: str
    method: str
    arguments: JsonValue = None
    kind: Literal["plugin"] = field(default="plugin", init=False)


@dataclass(frozen=True)
class TaskSpec:
    target: ScheduledAgent | ScheduledService
    schedule: TaskSchedule
    title: str | None = None


@dataclass(frozen=True)
class ScheduledTask:
    task_id: str
    revision: int
    configuration: TaskSpec
    enabled: bool
    status: str
    next_run_at: datetime | None
    active_execution_id: str | None
    error: str | None


@dataclass(frozen=True)
class ScheduledExecution:
    execution_id: str
    task_id: str
    scheduled_at: datetime
    status: str
    ended_at: datetime | None
    result: JsonValue
    error: str | None
    session_id: str | None = None
    run_id: str | None = None


class PluginScheduler(Protocol):
    async def create(self, configuration: TaskSpec) -> ScheduledTask: ...

    async def update(
        self, task_id: str, configuration: TaskSpec, *, expected_revision: int
    ) -> ScheduledTask: ...

    async def get(self, task_id: str) -> ScheduledTask: ...

    async def list(self, *, limit: int = 200, offset: int = 0) -> tuple[ScheduledTask, ...]: ...

    async def set_enabled(
        self, task_id: str, enabled: bool, *, expected_revision: int
    ) -> ScheduledTask: ...

    async def delete(self, task_id: str, *, expected_revision: int) -> None: ...

    async def executions(
        self, task_id: str, *, limit: int = 50
    ) -> tuple[ScheduledExecution, ...]: ...

    async def interrupt(self, task_id: str, *, expected_revision: int) -> None: ...
