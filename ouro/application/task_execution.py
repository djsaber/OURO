from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime

from ouro.application.supervisor import RuntimeSupervisor
from ouro.application.supervisor_models import RunHandle, RuntimeStatusSnapshot
from ouro.application.tasks import TaskService
from ouro.automation.models import TaskConflictError, TaskExecution, TaskRecord, task_status_for_run
from ouro.core.approvals import SessionApprovalMode
from ouro.core.base import new_id
from ouro.core.callbacks import run_blocking
from ouro.core.events import EventRecord
from ouro.core.interactions import SessionInteractionMode
from ouro.core.session_controls import HostSessionInput
from ouro.core.state import RunRecord

_BOUNDARIES = frozenset(
    {
        "run_started",
        "run_status_changed",
        "run_completed",
        "run_failed",
        "run_aborted",
        "run_paused",
        "approval_requested",
        "approval_resolved",
        "ask_user_requested",
        "ask_user_resolved",
    }
)


class TaskExecutionCoordinator:
    """Dispatch application task inputs and reconcile their canonical Run outcomes."""

    def __init__(
        self, tasks: TaskService, supervisor: RuntimeSupervisor, *, max_concurrency: int = 1
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("Task concurrency must be positive")
        self.tasks = tasks
        self.supervisor = supervisor
        self.runtime = tasks.runtime
        self.max_concurrency = max_concurrency
        self._handles: dict[str, RunHandle] = {}
        self._waiters: dict[str, asyncio.Task[None]] = {}
        self._completed: dict[str, RuntimeStatusSnapshot] = {}
        self._sessions: set[str] = set()
        self._due_cursor: tuple[datetime, str] | None = None
        self._registration = self.runtime.add_event_sink(self._on_event)

    async def _on_event(self, event: EventRecord) -> None:
        if event.session_id in self._sessions and event.event_type in _BOUNDARIES:
            self.tasks.changed.set()

    async def recover(self) -> None:
        executions = await run_blocking(self.tasks.repository.active)
        self._sessions = {execution.session_id for execution in executions}
        for execution in executions:
            # Canonical recovery checks Run ownership and preserves pending approvals.
            try:
                await self.supervisor.recover_session(execution.session_id)
            except FileNotFoundError:
                pass  # The exact occurrence is marked interrupted by reconciliation.
        await self.reconcile()

    async def tick(self) -> None:
        await self.reconcile()
        capacity = self.max_concurrency - len(self._handles)
        if capacity <= 0:
            return
        due = await run_blocking(
            lambda: self.tasks.repository.due(
                self.tasks.clock(),
                limit=100,
                after=self._due_cursor,
            )
        )
        if not due and self._due_cursor is not None:
            self._due_cursor = None
            due = await run_blocking(
                lambda: self.tasks.repository.due(self.tasks.clock(), limit=100)
            )
        for task in due:
            if len(self._handles) >= self.max_concurrency:
                break
            assert task.next_run_at is not None
            # Rotate the bounded admission window so busy reusable Sessions
            # cannot starve later tasks in a large due backlog.
            self._due_cursor = (task.next_run_at, task.task_id)
            try:
                await self._dispatch(task)
            except TaskConflictError:
                # A configuration edit won the atomic claim. Re-read the due index.
                self.tasks.changed.set()

    async def _dispatch(self, task: TaskRecord) -> None:
        session_id = task.session_id if task.configuration.session_mode == "reuse" else None
        existing = False
        if session_id is not None:
            try:
                session = await run_blocking(self.runtime.store.load_session_record, session_id)
            except FileNotFoundError:
                session_id = None
            else:
                # A user may be running or approving work in the reusable Session.
                if session.active_run_id is not None or await run_blocking(
                    self.runtime.store.is_session_locked, session_id
                ):
                    return
                existing = True
        session_id = session_id or new_id("ses")
        execution = await self.tasks.claim(task, session_id)
        if execution is None:
            return
        self._sessions.add(session_id)
        try:
            config = await run_blocking(self.tasks.validate, task.configuration)
            approval: SessionApprovalMode = (
                "auto" if config.approval_mode == "afk" else config.approval_mode
            )
            interaction: SessionInteractionMode = (
                "unattended" if config.approval_mode == "afk" else "interactive"
            )
            if not existing:
                await run_blocking(
                    lambda: self.runtime.create_session(
                        title=config.display_title,
                        _session_id=session_id,
                        namespace="task",
                        initial_model=config.model,
                        reasoning_request=config.reasoning,
                        workspace=config.workspace,
                        approval_mode=approval,
                        interaction_mode=interaction,
                    )
                )
            run = await run_blocking(
                lambda: self.runtime.prepare_run(
                    RunRecord(
                        run_id=execution.run_id,
                        session_id=session_id,
                        origin="host",
                        trigger="scheduled",
                        initiator_id=f"ouro.tasks/{task.task_id}",
                    ),
                    model=config.model,
                    reasoning=config.reasoning,
                    workspace=config.workspace,
                    approval_mode=approval,
                    interaction_mode=interaction,
                    title=config.display_title,
                )
            )
            handle = await self.supervisor.submit_prepared(
                run,
                HostSessionInput(
                    prompt=config.description,
                    role="user",
                    title=config.display_title,
                    host_source_id=f"ouro.tasks/{task.task_id}",
                ),
                background=True,
            )
        except Exception as exc:  # task launch is an isolation boundary
            await self._fail_launch(execution, exc)
            return
        self._handles[execution.execution_id] = handle
        self._waiters[execution.execution_id] = asyncio.create_task(
            self._wait(execution.execution_id, handle),
            name=f"ouro-task-run-{task.task_id}",
        )

    async def _fail_launch(self, execution: TaskExecution, error: Exception) -> None:
        # A reserved Run must be settled when dispatch fails, so it cannot hold
        # the Session busy forever. No execution is automatically replayed.
        with suppress(FileNotFoundError):
            run = await run_blocking(
                self.runtime.store.load_run, execution.session_id, execution.run_id
            )
            if run.status == "queued":
                await self.runtime.abort_run(
                    session_id=run.session_id, run_id=run.run_id, reason="Task dispatch failed"
                )
        now = self.tasks.clock()
        await self.tasks.record_execution(
            execution.model_copy(
                update={
                    "status": "failed",
                    "error": f"Task launch failed: {type(error).__name__}: {error}",
                    "updated_at": now,
                    "ended_at": now,
                }
            )
        )

    async def _wait(self, execution_id: str, handle: RunHandle) -> None:
        try:
            self._completed[execution_id] = await self.supervisor.wait(handle)
        finally:
            self.tasks.changed.set()

    async def reconcile(self) -> None:
        executions = await run_blocking(self.tasks.repository.active)
        self._sessions = {execution.session_id for execution in executions}
        for execution in executions:
            completed = self._completed.get(execution.execution_id)
            awaiting_approval = False
            if execution.execution_id in self._handles and completed is None:
                snapshot = self.supervisor.status(self._handles[execution.execution_id])
                status, error = task_status_for_run(snapshot.status), None
                awaiting_approval = snapshot.status == "waiting_approval"
                if status in {"completed", "failed", "interrupted"}:
                    # Terminal events precede producer completion. Keep the
                    # occurrence active until the supervised job releases it.
                    status = "running"
            elif completed is not None and completed.status == "failed":
                status, error = (
                    "failed",
                    "; ".join(item.message for item in completed.diagnostics) or "Run failed",
                )
            else:
                try:
                    run = await run_blocking(
                        self.runtime.store.load_run, execution.session_id, execution.run_id
                    )
                    status, error = task_status_for_run(run.status), run.error
                    awaiting_approval = run.status == "waiting_approval"
                    if run.status == "queued" and execution.execution_id not in self._handles:
                        status, error = (
                            "interrupted",
                            "Process stopped before dispatch; occurrence was not replayed",
                        )
                except FileNotFoundError:
                    status, error = (
                        "interrupted",
                        "Session or Run was removed before execution completed",
                    )
            finished = status in {"completed", "failed", "interrupted"}
            if (
                status != execution.status
                or awaiting_approval != execution.awaiting_approval
                or error != execution.error
                or finished
            ):
                now = self.tasks.clock()
                await self.tasks.record_execution(
                    execution.model_copy(
                        update={
                            "status": status,
                            "awaiting_approval": awaiting_approval,
                            "error": error,
                            "updated_at": now,
                            "ended_at": now if finished else None,
                        }
                    )
                )
            if completed is not None:
                handle = self._handles.pop(execution.execution_id)
                waiter = self._waiters.pop(execution.execution_id)
                await waiter
                self._completed.pop(execution.execution_id, None)
                self.supervisor.release_handle(handle)

    async def close(self, *, reconcile: bool = True) -> None:
        await self._registration.close()
        # Supervisor settles execution first. Receipt watchers then persist the
        # final result and release process-local data, including for parked Runs.
        await asyncio.gather(*self._waiters.values())
        if reconcile:
            await self.reconcile()
