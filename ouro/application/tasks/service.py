from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ouro.application.contracts.models import AvailableModel
from ouro.application.runtime.service import RuntimeService
from ouro.automation.plugin_tasks import BoundPluginTask, PluginTaskProvider
from ouro.automation.repository import TaskRepository
from ouro.automation.schedule import next_occurrence
from ouro.core.base import new_id, utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.model_selection import DEFAULT_MODEL
from ouro.core.tasks import (
    AgentTaskRun,
    AgentTaskTarget,
    PluginTaskCall,
    PluginTaskTarget,
    TaskConfiguration,
    TaskConflictError,
    TaskCounts,
    TaskExecution,
    TaskRecord,
)


class TaskService:
    """Validate task commands and expose durable configuration and execution queries."""

    def __init__(
        self,
        repository: TaskRepository,
        runtime: RuntimeService,
        *,
        clock: Callable[[], datetime] = utc_now,
        plugin_tasks: PluginTaskProvider | None = None,
    ) -> None:
        self.repository = repository
        self.runtime = runtime
        self.clock = clock
        self.plugin_tasks = plugin_tasks
        self.changed = asyncio.Event()
        self._initialized = False
        self._initialization = asyncio.Lock()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialization:
            if not self._initialized:
                await run_blocking(self.repository.initialize)
                self._initialized = True

    def notify_plugin_change(self) -> None:
        self.changed.set()

    async def create(
        self, configuration: TaskConfiguration, *, owner_id: str | None = None
    ) -> TaskRecord:
        self.require_owner(owner_id, configuration)
        configuration = await self.validate(configuration)
        await self.initialize()
        now = self.clock()
        due = next_occurrence(configuration.schedule, now, inclusive=True)
        if due is None:
            raise ValueError("Execution plan has no future occurrence")
        task = TaskRecord(
            task_id=new_id("task"),
            owner_id=owner_id,
            configuration=configuration,
            created_at=now,
            updated_at=now,
            next_run_at=due,
        )
        await run_blocking(lambda: self.repository.commit(task, expected_revision=None))
        self.changed.set()
        return task

    async def bind_plugin_task(self, target: PluginTaskTarget) -> BoundPluginTask:
        if self.plugin_tasks is None:
            raise ValueError("Plugin task execution is unavailable")
        return await self.plugin_tasks.bind_task(target)

    def owner_available(self, owner_id: str | None) -> bool:
        return owner_id is None or (
            self.plugin_tasks is not None and self.plugin_tasks.task_owner_available(owner_id)
        )

    def require_owner(self, owner_id: str | None, configuration: TaskConfiguration) -> None:
        if owner_id is None:
            return
        if self.plugin_tasks is None:
            raise ValueError("Plugin task ownership is unavailable")
        self.plugin_tasks.require_task_owner(owner_id, configuration.target)

    async def validate(self, configuration: TaskConfiguration) -> TaskConfiguration:
        # Validate at both save and dispatch: workspace/model availability may change.
        configuration = TaskConfiguration.model_validate(configuration.model_dump())
        if isinstance(configuration.target, PluginTaskTarget):
            binding = await self.bind_plugin_task(configuration.target)
            binding.close()
            return configuration
        return await run_blocking(self._validate_agent, configuration)

    def _validate_agent(self, configuration: TaskConfiguration) -> TaskConfiguration:
        target = configuration.target
        assert isinstance(target, AgentTaskTarget)
        workspace = Path(target.workspace).expanduser()
        if not workspace.is_absolute() or not workspace.is_dir():
            raise ValueError("Workspace must be an existing absolute directory")
        model = self.resolve_model(target)
        return configuration.model_copy(
            update={
                "target": target.model_copy(
                    update={
                        "workspace": str(workspace.resolve()),
                        "model": DEFAULT_MODEL if target.model == DEFAULT_MODEL else model.key,
                    }
                ),
            }
        )

    def resolve_model(self, target: AgentTaskTarget) -> AvailableModel:
        selector = (
            self.runtime.configured_default_model().key
            if target.model == DEFAULT_MODEL
            else target.model
        )
        return self.runtime.model_reasoning(selector, target.reasoning).model

    async def update(
        self,
        task_id: str,
        configuration: TaskConfiguration,
        *,
        expected_revision: int,
    ) -> TaskRecord:
        task = await self.get(task_id)
        self.require_owner(task.owner_id, configuration)
        configuration = await self.validate(configuration)
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
        self, task_id: str, enabled: bool, *, expected_revision: int, error: str | None = None
    ) -> TaskRecord:
        task = await self.get(task_id)
        if enabled:
            self.require_owner(task.owner_id, task.configuration)
            await self.validate(task.configuration)
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
                "error": error,
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
        await self.initialize()
        await run_blocking(
            lambda: self.repository.delete(task_id, expected_revision=expected_revision)
        )
        self.changed.set()

    async def get(self, task_id: str) -> TaskRecord:
        await self.initialize()
        return await run_blocking(self.repository.get, task_id)

    async def list(
        self, *, limit: int = 200, offset: int = 0, owner_id: str | None = None
    ) -> tuple[TaskRecord, ...]:
        await self.initialize()
        return await run_blocking(
            lambda: self.repository.list(limit=limit, offset=offset, owner_id=owner_id)
        )

    async def counts(self) -> TaskCounts:
        await self.initialize()
        return await run_blocking(self.repository.counts)

    async def executions(self, task_id: str, *, limit: int = 50) -> tuple[TaskExecution, ...]:
        await self.initialize()
        return await run_blocking(lambda: self.repository.executions(task_id, limit=limit))

    async def request_interrupt(self, task: TaskRecord) -> TaskExecution | None:
        if task.active_execution_id is None:
            return None
        entries = await self.executions(task.task_id, limit=1)
        if not entries or entries[0].execution_id != task.active_execution_id:
            raise TaskConflictError("Task execution changed; reload it before interrupting")
        now = self.clock()
        updated = task.model_copy(update={"revision": task.revision + 1, "updated_at": now})
        execution = entries[0].model_copy(update={"cancel_requested": True, "updated_at": now})
        await run_blocking(
            lambda: self.repository.commit(
                updated,
                expected_revision=task.revision,
                execution=execution,
            )
        )
        self.changed.set()
        return execution

    async def claim(
        self, task: TaskRecord, target: AgentTaskRun | PluginTaskCall
    ) -> TaskExecution | None:
        if task.configuration.target.kind != target.kind:
            raise ValueError("Execution target must match the task target kind")
        if isinstance(target, PluginTaskCall) and target.request != task.configuration.target:
            raise ValueError("Execution target must bind the task's accepted request")
        await self.initialize()
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
            owner_id=task.owner_id,
            scheduled_at=task.next_run_at,
            created_at=now,
            updated_at=now,
            target=target,
        )
        updated = task.model_copy(
            update={
                "revision": task.revision + 1,
                "updated_at": now,
                "session_id": target.session_id if isinstance(target, AgentTaskRun) else None,
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
