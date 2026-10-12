from __future__ import annotations

from typing import Protocol

from ouro.core.tasks import TaskConfiguration, TaskExecution, TaskRecord


class TaskServicePort(Protocol):
    """Task commands and queries available to a deployment's plugin adapter."""

    def notify_plugin_change(self) -> None: ...

    async def create(
        self, configuration: TaskConfiguration, *, owner_id: str | None = None
    ) -> TaskRecord: ...

    async def update(
        self, task_id: str, configuration: TaskConfiguration, *, expected_revision: int
    ) -> TaskRecord: ...

    async def get(self, task_id: str) -> TaskRecord: ...

    async def list(
        self, *, limit: int = 200, offset: int = 0, owner_id: str | None = None
    ) -> tuple[TaskRecord, ...]: ...

    async def set_enabled(
        self, task_id: str, enabled: bool, *, expected_revision: int
    ) -> TaskRecord: ...

    async def delete(self, task_id: str, *, expected_revision: int) -> None: ...

    async def executions(self, task_id: str, *, limit: int = 50) -> tuple[TaskExecution, ...]: ...

    async def request_interrupt(self, task: TaskRecord) -> TaskExecution | None: ...
