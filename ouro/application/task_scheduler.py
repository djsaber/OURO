from __future__ import annotations

import asyncio
import logging

from ouro.application.dto import Diagnostic
from ouro.application.task_execution import TaskExecutionCoordinator
from ouro.application.tasks import TaskService
from ouro.core.callbacks import run_blocking

_LOG = logging.getLogger(__name__)


class TaskScheduler:
    """Own one deadline-driven task dispatcher per deployment home."""

    def __init__(
        self,
        tasks: TaskService,
        execution: TaskExecutionCoordinator,
        *,
        refresh_seconds: float = 30,
    ) -> None:
        if refresh_seconds <= 0:
            raise ValueError("Task refresh interval must be positive")
        self.tasks = tasks
        self.execution = execution
        self.refresh_seconds = refresh_seconds
        self._runner: asyncio.Task[None] | None = None
        self._stopping = False
        self._owner = False
        self._initialized = False
        self._recovered = False
        self.diagnostics: tuple[Diagnostic, ...] = ()

    async def start(self) -> None:
        if self._runner is not None:
            return
        try:
            await self._initialize()
        except Exception as exc:
            self._report_failure(exc)
        self._runner = asyncio.create_task(self._run(), name="ouro-task-scheduler")

    async def _initialize(self) -> None:
        if not self._initialized:
            await run_blocking(self.tasks.repository.initialize)
            self._initialized = True

    async def _run(self) -> None:
        while not self._stopping:
            self.tasks.changed.clear()
            delay = self.refresh_seconds
            try:
                await self._initialize()
                if not self._owner:
                    self._owner = await run_blocking(self.tasks.repository.acquire_scheduler)
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

    async def close(self) -> None:
        try:
            await self.stop()
            await self.execution.close(reconcile=self._owner)
        finally:
            await run_blocking(self.tasks.repository.release_scheduler)
            self._owner = False
