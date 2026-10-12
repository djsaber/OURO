from __future__ import annotations

from datetime import time
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, ConfigDict, Field, JsonValue, field_validator, model_validator

from ouro.core.base import OuroModel
from ouro.core.plugin_tasks import PluginTaskCall, PluginTaskTarget
from ouro.core.reasoning import ReasoningRequest

TaskStatus = Literal["waiting", "running", "paused", "completed", "failed", "interrupted"]
TaskApprovalPreset = Literal["afk", "inherit", "auto", "approval", "deny"]
ScheduleFrequency = Literal["hourly", "daily", "weekdays", "weekly", "monthly", "custom"]
RepeatUnit = Literal["minutes", "hours", "days", "weeks", "months"]


class TaskSchedule(OuroModel):
    """Calendar recurrence anchored to a local start, with UTC occurrence identities."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    frequency: ScheduleFrequency
    timezone: str = "UTC"
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None = None
    at: time = time(9)
    interval: int = Field(default=1, ge=1, le=10000, strict=True)
    unit: RepeatUnit = "days"
    weekdays: tuple[int, ...] = ()  # Monday = 0; also filters custom calendar schedules.
    month_day: int | None = Field(default=None, ge=1, le=31, strict=True)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown IANA time zone: {value}") from exc
        return value

    @model_validator(mode="after")
    def valid_schedule(self) -> Self:
        if self.ends_at is not None and self.ends_at < self.starts_at:
            raise ValueError("End must be on or after start")
        if self.at.tzinfo is not None or self.at.second or self.at.microsecond:
            raise ValueError("Execution time must be local HH:MM, without a time zone")
        if len(set(self.weekdays)) != len(self.weekdays) or any(
            type(day) is not int or not 0 <= day <= 6 for day in self.weekdays
        ):
            raise ValueError("Weekdays must be distinct numbers from 0 (Monday) to 6 (Sunday)")
        if self.frequency != "custom" and (self.interval != 1 or self.unit != "days"):
            raise ValueError("Repeat unit and interval are only configurable for custom schedules")
        if self.weekdays and self.frequency not in {"weekly", "custom"}:
            raise ValueError("Weekday selection requires a weekly or custom schedule")
        if self.month_day is not None and not (
            self.frequency == "monthly" or (self.frequency == "custom" and self.unit == "months")
        ):
            raise ValueError("Day of month requires a monthly schedule")
        if self.frequency == "custom" and self.unit in {"minutes", "hours"} and self.weekdays:
            raise ValueError("Weekday filters require a calendar repeat unit")
        return self


class AgentTaskTarget(OuroModel):
    """Instructions and execution settings for a scheduled Agent Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["agent"] = "agent"
    prompt: str = Field(min_length=1, max_length=100000)
    workspace: str = Field(min_length=1)
    approval_mode: TaskApprovalPreset = "afk"
    model: str = Field(min_length=1)
    reasoning: ReasoningRequest = Field(default_factory=ReasoningRequest)
    session_mode: Literal["new", "reuse"] = "new"

    @field_validator("prompt", "workspace", "model", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class TaskConfiguration(OuroModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    title: str | None = Field(default=None, max_length=160)
    schedule: TaskSchedule
    target: AgentTaskTarget | PluginTaskTarget = Field(discriminator="kind")

    @field_validator("title", mode="before")
    @classmethod
    def strip_title(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        if isinstance(self.target, AgentTaskTarget):
            return " ".join(self.target.prompt.split())[:60]
        return f"{self.target.plugin_id}/{self.target.service}/{self.target.method}"


class TaskRecord(OuroModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str
    owner_id: str | None = Field(default=None, min_length=1)
    revision: int = Field(default=1, ge=1)
    configuration: TaskConfiguration
    enabled: bool = True
    created_at: AwareDatetime
    updated_at: AwareDatetime
    next_run_at: AwareDatetime | None
    session_id: str | None = None
    active_execution_id: str | None = None
    last_execution_id: str | None = None
    status: TaskStatus = "waiting"
    error: str | None = None


class AgentTaskRun(OuroModel):
    """Exact Run identity reserved for one claimed Agent task occurrence."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["agent"] = "agent"
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)


class TaskExecution(OuroModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    execution_id: str
    task_id: str
    owner_id: str | None = Field(default=None, min_length=1)
    scheduled_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime
    target: AgentTaskRun | PluginTaskCall = Field(discriminator="kind")
    result: JsonValue = None
    cancel_requested: bool = False
    status: TaskStatus = "waiting"
    awaiting_approval: bool = False
    error: str | None = None
    ended_at: AwareDatetime | None = None


class TaskCounts(OuroModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    waiting: int = 0
    running: int = 0
    paused: int = 0
    completed: int = 0
    failed: int = 0
    interrupted: int = 0
    awaiting_approval: int = 0  # Subset of paused executions; excludes disabled schedules.
    completed_executions: int = 0
    failed_executions: int = 0


class TaskConflictError(ValueError):
    """Configuration changed or an operation conflicts with active execution."""


def task_status_for_run(status: str) -> TaskStatus:
    if status in {"queued", "idle"}:
        return "waiting"
    if status in {"running", "cancelling", "waiting_child"}:
        return "running"
    if status in {"waiting_approval", "waiting_user", "paused"}:
        return "paused"
    if status in {"aborted", "recoverable"}:
        return "interrupted"
    if status == "completed":
        return "completed"
    if status == "failed":
        return "failed"
    raise ValueError(f"Unknown runtime status: {status}")
