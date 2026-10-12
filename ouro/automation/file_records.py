"""Versioned file records and the disposable task catalog projection."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, ConfigDict, Field

from ouro.core.base import OuroModel
from ouro.core.tasks import TaskExecution, TaskRecord, TaskStatus


class FileRecord(OuroModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[3] = 3


class StoredTask(FileRecord):
    task: TaskRecord
    history_head: str | None = None
    completed_executions: int = Field(default=0, ge=0)
    failed_executions: int = Field(default=0, ge=0)
    awaiting_approval: bool = False


class StoredExecution(FileRecord):
    execution: TaskExecution
    previous: str | None = None


class TaskIndexEntry(OuroModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    updated_at: AwareDatetime
    owner_id: str | None
    enabled: bool
    next_run_at: AwareDatetime | None
    active_execution_id: str | None
    status: TaskStatus
    completed_executions: int = Field(ge=0)
    failed_executions: int = Field(ge=0)
    awaiting_approval: bool

    @classmethod
    def from_record(cls, record: StoredTask) -> TaskIndexEntry:
        task = record.task
        return cls(
            updated_at=task.updated_at,
            owner_id=task.owner_id,
            enabled=task.enabled,
            next_run_at=task.next_run_at,
            active_execution_id=task.active_execution_id,
            status=task.status,
            completed_executions=record.completed_executions,
            failed_executions=record.failed_executions,
            awaiting_approval=record.awaiting_approval,
        )


class TaskFileIndex(FileRecord):
    tasks: dict[str, TaskIndexEntry] = Field(default_factory=dict)


class TaskFileTransaction(FileRecord):
    task_id: str
    record: StoredTask | None  # None deletes the task and its execution files.
    execution: StoredExecution | None = None
