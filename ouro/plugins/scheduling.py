from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict

from ouro.core.task_ports import TaskServicePort
from ouro.core.tasks import (
    AgentTaskRun,
    AgentTaskTarget,
    TaskConfiguration,
    TaskConflictError,
    TaskRecord,
)
from ouro.plugins.catalog.gates import GenerationGate
from ouro.plugins.invocation import current_invocation
from ouro.sdk.plugins import scheduling as sdk
from ouro.sdk.plugins.models import ModelReasoning


def task_spec(configuration: TaskConfiguration) -> sdk.TaskSpec:
    target = configuration.target.model_dump()
    target.pop("kind")
    action: sdk.ScheduledAgent | sdk.ScheduledService
    if isinstance(configuration.target, AgentTaskTarget):
        target["reasoning"] = ModelReasoning(**target["reasoning"])
        action = sdk.ScheduledAgent(**target)
    else:
        action = sdk.ScheduledService(**target)
    return sdk.TaskSpec(
        action, sdk.TaskSchedule(**configuration.schedule.model_dump()), configuration.title
    )


def scheduled_task(task: TaskRecord) -> sdk.ScheduledTask:
    return sdk.ScheduledTask(
        task.task_id,
        task.revision,
        task_spec(task.configuration),
        task.enabled,
        task.status,
        task.next_run_at,
        task.active_execution_id,
        task.error,
    )


class NativePluginScheduler:
    """A generation-scoped, owner-filtered view of the native task service."""

    def __init__(
        self,
        service: TaskServicePort,
        plugin_id: str,
        gate: GenerationGate,
        permissions: frozenset[str],
    ) -> None:
        self.service = service
        self.plugin_id = plugin_id
        self.gate = gate
        self.permissions = permissions

    @asynccontextmanager
    async def _scope(self, *, writable: bool) -> AsyncIterator[None]:
        invocation = current_invocation.get()
        if invocation is not None:
            invocation.require_active()
            if writable and not invocation.writable:
                raise PermissionError("A read-only callback cannot change schedules")
        if writable and "background.run" not in self.permissions:
            raise PermissionError("Changing schedules requires background.run")
        async with self.gate.invocation():
            yield

    async def _owned(self, task_id: str, revision: int | None = None) -> TaskRecord:
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ValueError("Expected revision must be a positive integer")
        task = await self.service.get(task_id)
        if task.owner_id != self.plugin_id:
            raise PermissionError("Task belongs to another owner")
        if revision is not None and task.revision != revision:
            raise TaskConflictError("Task changed; reload it before saving")
        return task

    async def create(self, configuration: sdk.TaskSpec) -> sdk.ScheduledTask:
        async with self._scope(writable=True):
            task = await self.service.create(
                TaskConfiguration.model_validate(asdict(configuration)),
                owner_id=self.plugin_id,
            )
            return scheduled_task(task)

    async def update(
        self, task_id: str, configuration: sdk.TaskSpec, *, expected_revision: int
    ) -> sdk.ScheduledTask:
        async with self._scope(writable=True):
            await self._owned(task_id, expected_revision)
            task = await self.service.update(
                task_id,
                TaskConfiguration.model_validate(asdict(configuration)),
                expected_revision=expected_revision,
            )
            return scheduled_task(task)

    async def get(self, task_id: str) -> sdk.ScheduledTask:
        async with self._scope(writable=False):
            return scheduled_task(await self._owned(task_id))

    async def list(self, *, limit: int = 200, offset: int = 0) -> tuple[sdk.ScheduledTask, ...]:
        async with self._scope(writable=False):
            tasks = await self.service.list(limit=limit, offset=offset, owner_id=self.plugin_id)
            return tuple(scheduled_task(task) for task in tasks)

    async def set_enabled(
        self, task_id: str, enabled: bool, *, expected_revision: int
    ) -> sdk.ScheduledTask:
        async with self._scope(writable=True):
            if type(enabled) is not bool:
                raise ValueError("Enabled must be a boolean")
            await self._owned(task_id, expected_revision)
            task = await self.service.set_enabled(
                task_id, enabled, expected_revision=expected_revision
            )
            return scheduled_task(task)

    async def delete(self, task_id: str, *, expected_revision: int) -> None:
        async with self._scope(writable=True):
            await self._owned(task_id, expected_revision)
            await self.service.delete(task_id, expected_revision=expected_revision)

    async def executions(
        self, task_id: str, *, limit: int = 50
    ) -> tuple[sdk.ScheduledExecution, ...]:
        async with self._scope(writable=False):
            await self._owned(task_id)
            executions = await self.service.executions(task_id, limit=limit)
            return tuple(
                sdk.ScheduledExecution(
                    item.execution_id,
                    item.task_id,
                    item.scheduled_at,
                    item.status,
                    item.ended_at,
                    item.result,
                    item.error,
                    item.target.session_id if isinstance(item.target, AgentTaskRun) else None,
                    item.target.run_id if isinstance(item.target, AgentTaskRun) else None,
                )
                for item in executions
            )

    async def interrupt(self, task_id: str, *, expected_revision: int) -> None:
        async with self._scope(writable=True):
            task = await self._owned(task_id, expected_revision)
            await self.service.request_interrupt(task)
