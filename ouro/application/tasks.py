from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ouro.application.models import AvailableModel
from ouro.application.runtime import RuntimeService
from ouro.automation.models import (
    TaskConfiguration,
    TaskConflictError,
    TaskCounts,
    TaskExecution,
    TaskRecord,
)
from ouro.automation.repository import TaskRepository
from ouro.automation.schedule import next_occurrence
from ouro.core.base import new_id, utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.model_selection import DEFAULT_MODEL


class TaskService:
    """Validate task commands and expose durable configuration and execution queries."""

    def __init__(
        self,
        repository: TaskRepository,
        runtime: RuntimeService,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repository = repository
        self.runtime = runtime
        self.clock = clock
        self.changed = asyncio.Event()

    async def create(self, configuration: TaskConfiguration) -> TaskRecord:
        configuration = await run_blocking(self.validate, configuration)
        now = self.clock()
        due = next_occurrence(configuration.schedule, now, inclusive=True)
        if due is None:
            raise ValueError("Execution plan has no future occurrence")
        task = TaskRecord(
            task_id=new_id("task"),
            configuration=configuration,
            created_at=now,
            updated_at=now,
            next_run_at=due,
        )
        await run_blocking(lambda: self.repository.commit(task, expected_revision=None))
        self.changed.set()
        return task

    def validate(self, configuration: TaskConfiguration) -> TaskConfiguration:
        # Validate at both save and dispatch: workspace/model availability may change.
        configuration = TaskConfiguration.model_validate(configuration.model_dump())
        workspace = Path(configuration.workspace).expanduser()
        if not workspace.is_absolute() or not workspace.is_dir():
            raise ValueError("Workspace must be an existing absolute directory")
        model = self.resolve_model(configuration)
        return configuration.model_copy(
            update={
                "workspace": str(workspace.resolve()),
                "model": DEFAULT_MODEL if configuration.model == DEFAULT_MODEL else model.key,
            }
        )

    def resolve_model(self, configuration: TaskConfiguration) -> AvailableModel:
        selector = (
            self.runtime.configured_default_model().key
            if configuration.model == DEFAULT_MODEL
            else configuration.model
        )
        return self.runtime.model_reasoning(selector, configuration.reasoning).model

    async def update(
        self,
        task_id: str,
        configuration: TaskConfiguration,
        *,
        expected_revision: int,
    ) -> TaskRecord:
        configuration = await run_blocking(self.validate, configuration)
        task = await self.get(task_id)
        now = self.clock()
        due = task.next_run_at
        if configuration.schedule != task.configuration.schedule:
            due = next_occurrence(configuration.schedule, now, inclusive=True)
            if due is None:
                raise ValueError("Execution plan has no future occurrence")
        updated = task.model_copy(
            update={
                "configuration": configuration,
                "revision": expected_revision + 1,
                "updated_at": now,
                "next_run_at": due,
                "status": "waiting"
                if task.enabled and task.active_execution_id is None and due is not None
                else task.status,
            }
        )
        await run_blocking(
            lambda: self.repository.commit(updated, expected_revision=expected_revision)
        )
        self.changed.set()
        return updated

    async def set_enabled(
        self, task_id: str, enabled: bool, *, expected_revision: int
    ) -> TaskRecord:
        task = await self.get(task_id)
        now = self.clock()
        due = (
            next_occurrence(task.configuration.schedule, now, inclusive=True)
            if enabled
            else task.next_run_at
        )
        if enabled and due is None:
            raise ValueError("Execution plan has ended; edit the schedule before enabling")
        updated = task.model_copy(
            update={
                "enabled": enabled,
                "revision": expected_revision + 1,
                "updated_at": now,
                "next_run_at": due,
                "status": task.status
                if task.active_execution_id
                else ("waiting" if enabled else "paused"),
            }
        )
        await run_blocking(
            lambda: self.repository.commit(updated, expected_revision=expected_revision)
        )
        self.changed.set()
        return updated

    async def delete(self, task_id: str, *, expected_revision: int) -> None:
        await run_blocking(
            lambda: self.repository.delete(task_id, expected_revision=expected_revision)
        )
        self.changed.set()

    async def get(self, task_id: str) -> TaskRecord:
        return await run_blocking(self.repository.get, task_id)

    async def list(self, *, limit: int = 200, offset: int = 0) -> tuple[TaskRecord, ...]:
        return await run_blocking(lambda: self.repository.list(limit=limit, offset=offset))

    async def counts(self) -> TaskCounts:
        return await run_blocking(self.repository.counts)

    async def executions(self, task_id: str, *, limit: int = 50) -> tuple[TaskExecution, ...]:
        return await run_blocking(lambda: self.repository.executions(task_id, limit=limit))

    async def claim(self, task: TaskRecord, session_id: str) -> TaskExecution | None:
        now = self.clock()
        if not task.enabled or task.active_execution_id or task.next_run_at is None:
            raise TaskConflictError(
                "Task is disabled, already executing, or has no scheduled occurrence"
            )
        if task.next_run_at > now:
            raise TaskConflictError("Task is not due")
        end = task.configuration.schedule.ends_at
        if end is not None and now > end:
            entries = await self.executions(task.task_id, limit=1)
            expired = task.model_copy(
                update={
                    "revision": task.revision + 1,
                    "updated_at": now,
                    "next_run_at": None,
                    "status": entries[0].status if entries else "completed",
                }
            )
            await run_blocking(
                lambda: self.repository.commit(expired, expected_revision=task.revision)
            )
            return None
        execution = TaskExecution(
            execution_id=new_id("execution"),
            task_id=task.task_id,
            scheduled_at=task.next_run_at,
            created_at=now,
            updated_at=now,
            session_id=session_id,
            run_id=new_id("run"),
        )
        updated = task.model_copy(
            update={
                "revision": task.revision + 1,
                "updated_at": now,
                "session_id": session_id,
                "active_execution_id": execution.execution_id,
                "last_execution_id": execution.execution_id,
                "status": "waiting",
                "error": None,
                # Coalesce missed occurrences; never build an unbounded catch-up queue.
                "next_run_at": next_occurrence(task.configuration.schedule, now),
            }
        )
        await run_blocking(
            lambda: self.repository.commit(
                updated,
                expected_revision=task.revision,
                execution=execution,
            )
        )
        return execution

    async def record_execution(self, execution: TaskExecution) -> None:
        # Concurrent UI edits keep their configuration; only the matching active
        # occurrence may change the task's execution projection.
        for _ in range(3):
            task = await self.get(execution.task_id)
            if task.active_execution_id != execution.execution_id:
                return
            finished = execution.ended_at is not None
            now = self.clock()
            due = task.next_run_at
            status = execution.status
            if finished:
                end = task.configuration.schedule.ends_at
                if end is not None and now > end:
                    due = None
                # The occurrence keeps its outcome; the task describes the
                # remaining schedule once its active execution is released.
                if not task.enabled:
                    status = "paused"
                elif due is not None:
                    status = "waiting"
            updated = task.model_copy(
                update={
                    "revision": task.revision + 1,
                    "updated_at": now,
                    "active_execution_id": None if finished else execution.execution_id,
                    "next_run_at": due,
                    "status": status,
                    "error": execution.error,
                }
            )
            try:
                await run_blocking(
                    lambda updated=updated, task=task: self.repository.commit(
                        updated,
                        expected_revision=task.revision,
                        execution=execution,
                    )
                )
                return
            except TaskConflictError:
                continue
        raise TaskConflictError("Task kept changing while recording execution; retry on next tick")
