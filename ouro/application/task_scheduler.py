from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from ouro.application.dto import Diagnostic
from ouro.application.task_execution import TaskExecutionCoordinator
from ouro.application.tasks import TaskService
from ouro.core.callbacks import run_blocking

_LOG = logging.getLogger(__name__)


class SchedulerOwnedServices(Protocol):
    async def start_home_services(self) -> None: ...

    async def stop_home_services(self) -> None: ...


class TaskScheduler:
    """Own one deadline-driven task dispatcher per deployment home."""

    def __init__(
        self,
        tasks: TaskService,
        execution: TaskExecutionCoordinator,
        *,
        refresh_seconds: float = 30,
        refresh_environment: Callable[[], Awaitable[None]] | None = None,
        owned_services: SchedulerOwnedServices | None = None,
    ) -> None:
        if refresh_seconds <= 0:
            raise ValueError("Task refresh interval must be positive")
        self.tasks = tasks
        self.execution = execution
        self.refresh_seconds = refresh_seconds
        self.refresh_environment = refresh_environment
        self.owned_services = owned_services
        self._runner: asyncio.Task[None] | None = None
        self._stopping = False
        self._owner = False
        self._recovered = False
        self.diagnostics: tuple[Diagnostic, ...] = ()

    async def start(self) -> None:
        if self._runner is not None:
            return
        try:
            await self.tasks.initialize()
        except Exception as exc:
            self._report_failure(exc)
        self._runner = asyncio.create_task(self._run(), name="ouro-task-scheduler")

    async def _run(self) -> None:
        while not self._stopping:
            self.tasks.changed.clear()
            delay = self.refresh_seconds
            try:
                if self.refresh_environment is not None:
                    await self.refresh_environment()
                await self.tasks.initialize()
                if not self._owner:
                    self._owner = await run_blocking(self.tasks.repository.acquire_scheduler)
                    if self._owner and self.owned_services is not None:
                        await self.owned_services.start_home_services()
                if self._owner:
                    if not self._recovered:
                        await self.execution.recover()
                        self._recovered = True
                    await self.execution.tick()
                    due = await run_blocking(self.tasks.repository.next_due)
                    if due is not None:
                        # A due but busy Session or exhausted execution capacity
                        # must not turn into a tight loop.
                        delay = min(delay, max(1.0, (due - self.tasks.clock()).total_seconds()))
                self.diagnostics = ()
            except Exception as exc:  # scheduling failure must not stop interactive hosts
                self._report_failure(exc)
            try:
                async with asyncio.timeout(delay):
                    await self.tasks.changed.wait()
            except TimeoutError:
                pass

    def _report_failure(self, error: Exception) -> None:
        diagnostic = Diagnostic(
            code="task_scheduler_failed",
            severity="error",
            component="tasks.scheduler",
            message=f"Task scheduler failed ({type(error).__name__}): {error}",
            recoverable=True,
            suggested_action="Check task storage and configuration; scheduling will retry.",
        )
        if not self.diagnostics or self.diagnostics[0].message != diagnostic.message:
            _LOG.error("%s", diagnostic.message)
        self.diagnostics = (diagnostic,)

    async def stop(self) -> None:
        self._stopping = True
        self.tasks.changed.set()
        if self._runner is not None:
            await self._runner
            self._runner = None
        await self.execution.stop_plugin_tasks(reconcile=self._owner)
        if self._owner and self.owned_services is not None:
            await self.owned_services.stop_home_services()

    async def close(self) -> None:
        # A failed service stop retains the scheduler lock. Another Host must
        # not acquire singleton ownership while this Host still owns the work.
        await self.stop()
        try:
            await self.execution.close(reconcile=self._owner)
        finally:
            await run_blocking(self.tasks.repository.release_scheduler)
            self._owner = False
