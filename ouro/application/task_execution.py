from __future__ import annotations

import asyncio
from contextlib import suppress
from contextvars import Context
from datetime import datetime

from ouro.application.supervisor import RuntimeSupervisor
from ouro.application.supervisor_models import RunHandle, RuntimeStatusSnapshot
from ouro.application.tasks import TaskService
from ouro.automation.plugin_tasks import BoundPluginTask
from ouro.core.approvals import SessionApprovalMode
from ouro.core.base import new_id
from ouro.core.callbacks import run_blocking
from ouro.core.events import EventRecord
from ouro.core.interactions import SessionInteractionMode
from ouro.core.redaction import redact_text
from ouro.core.session_controls import HostSessionInput
from ouro.core.state import RunRecord
from ouro.core.tasks import (
    AgentTaskRun,
    AgentTaskTarget,
    PluginTaskCall,
    PluginTaskTarget,
    TaskConflictError,
    TaskExecution,
    TaskRecord,
    task_status_for_run,
)

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
        self._plugin_jobs: dict[str, asyncio.Task[TaskExecution]] = {}
        self._sessions: set[str] = set()
        self._due_cursor: tuple[datetime, str] | None = None
        self._registration = self.runtime.add_event_sink(self._on_event)

    async def _on_event(self, event: EventRecord) -> None:
        if event.session_id in self._sessions and event.event_type in _BOUNDARIES:
            self.tasks.changed.set()

    async def recover(self) -> None:
        executions = await run_blocking(self.tasks.repository.active)
        self._sessions = {
            execution.target.session_id
            for execution in executions
            if isinstance(execution.target, AgentTaskRun)
        }
        for execution in executions:
            if not isinstance(execution.target, AgentTaskRun):
                continue
            # Canonical recovery checks Run ownership and preserves pending approvals.
            try:
                await self.supervisor.recover_session(execution.target.session_id)
            except FileNotFoundError:
                pass  # The exact occurrence is marked interrupted by reconciliation.
        await self.reconcile()

    async def tick(self) -> None:
        await self.reconcile()
        capacity = self.max_concurrency - len(self._handles) - len(self._plugin_jobs)
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
            if len(self._handles) + len(self._plugin_jobs) >= self.max_concurrency:
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
        try:
            self.tasks.require_owner(task.owner_id, task.configuration)
        except (ValueError, PermissionError) as exc:
            await self.tasks.set_enabled(
                task.task_id,
                False,
                expected_revision=task.revision,
                error=str(exc),
            )
            return
        if isinstance(task.configuration.target, PluginTaskTarget):
            await self._dispatch_plugin(task, task.configuration.target)
            return
        session_id = task.session_id if task.configuration.target.session_mode == "reuse" else None
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
        execution = await self.tasks.claim(
            task, AgentTaskRun(session_id=session_id, run_id=new_id("run"))
        )
        if execution is None:
            return
        self._sessions.add(session_id)
        try:
            config = await self.tasks.validate(task.configuration)
            target = config.target
            assert isinstance(target, AgentTaskTarget)
            assert isinstance(execution.target, AgentTaskRun)
            run_id = execution.target.run_id
            model_route = (await run_blocking(self.tasks.resolve_model, target)).key
            approval: SessionApprovalMode = (
                "auto" if target.approval_mode == "afk" else target.approval_mode
            )
            interaction: SessionInteractionMode = (
                "unattended" if target.approval_mode == "afk" else "interactive"
            )
            if not existing:
                await run_blocking(
                    lambda: self.runtime.create_session(
                        title=config.display_title,
                        _session_id=session_id,
                        namespace="task",
                        initial_model=model_route,
                        reasoning_request=target.reasoning,
                        workspace=target.workspace,
                        approval_mode=approval,
                        interaction_mode=interaction,
                    )
                )
            run = await run_blocking(
                lambda: self.runtime.prepare_run(
                    RunRecord(
                        run_id=run_id,
                        session_id=session_id,
                        origin="host",
                        trigger="scheduled",
                        initiator_id=f"ouro.tasks/{task.task_id}",
                    ),
                    model=model_route,
                    reasoning=target.reasoning,
                    workspace=target.workspace,
                    approval_mode=approval,
                    interaction_mode=interaction,
                    title=config.display_title,
                )
            )
            handle = await self.supervisor.submit_prepared(
                run,
                HostSessionInput(
                    prompt=target.prompt,
                    role="user",
                    semantic_tag="scheduled-task",
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
        assert isinstance(execution.target, AgentTaskRun)
        with suppress(FileNotFoundError):
            run = await run_blocking(
                self.runtime.store.load_run, execution.target.session_id, execution.target.run_id
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
        self._sessions = {
            execution.target.session_id
            for execution in executions
            if isinstance(execution.target, AgentTaskRun)
        }
        for execution in executions:
            if isinstance(execution.target, PluginTaskCall):
                if not self.tasks.owner_available(execution.owner_id):
                    job = self._plugin_jobs.get(execution.execution_id)
                    if job is not None:
                        job.cancel()
                await self._reconcile_plugin(execution)
                continue
            if execution.cancel_requested or not self.tasks.owner_available(execution.owner_id):
                await self._interrupt_agent(
                    execution,
                    reason="Task interrupt requested"
                    if execution.cancel_requested
                    else "Task owner is unavailable",
                )
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
                        self.runtime.store.load_run,
                        execution.target.session_id,
                        execution.target.run_id,
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

    async def _dispatch_plugin(self, task: TaskRecord, target: PluginTaskTarget) -> None:
        try:
            binding = await self.tasks.bind_plugin_task(target)
        except Exception as exc:
            # An unavailable declaration pauses its plan; it does not consume
            # occurrences repeatedly or activate a disabled plugin.
            await self.tasks.set_enabled(
                task.task_id,
                False,
                expected_revision=task.revision,
                error=f"Plugin task unavailable: {redact_text(str(exc))[:4096]}",
            )
            return
        try:
            execution = await self.tasks.claim(task, binding.target)
            if execution is None:
                binding.close()
                return
            job = asyncio.create_task(
                self._run_plugin(execution, binding),
                name=f"ouro-plugin-task-{task.task_id}",
                context=Context(),
            )
            self._plugin_jobs[execution.execution_id] = job

            def finished(_: asyncio.Task[TaskExecution]) -> None:
                # Also releases a job cancelled before its coroutine starts.
                binding.close()
                self.tasks.changed.set()

            job.add_done_callback(finished)
            self.tasks.changed.set()
        except BaseException:
            binding.close()
            raise

    async def _run_plugin(
        self, execution: TaskExecution, binding: BoundPluginTask
    ) -> TaskExecution:
        result = None
        status = "completed"
        error = None
        try:
            result = await binding.invoke(
                task_id=execution.task_id,
                execution_id=execution.execution_id,
                scheduled_at=execution.scheduled_at,
            )
        except asyncio.CancelledError:
            status, error = (
                "interrupted",
                "Plugin task execution was cancelled; occurrence was not replayed",
            )
        except Exception as exc:
            status, error = "failed", f"{type(exc).__name__}: {redact_text(str(exc))[:4096]}"
        now = self.tasks.clock()
        return execution.model_copy(
            update={
                "status": status,
                "result": result,
                "error": error,
                "ended_at": now,
                "updated_at": now,
            }
        )

    async def _reconcile_plugin(self, execution: TaskExecution) -> None:
        job = self._plugin_jobs.get(execution.execution_id)
        now = self.tasks.clock()
        if job is not None and not job.done():
            if execution.cancel_requested:
                job.cancel()
            if execution.status != "running":
                await self.tasks.record_execution(
                    execution.model_copy(update={"status": "running", "updated_at": now})
                )
            return
        if job is None or job.cancelled():
            settled = execution.model_copy(
                update={
                    "status": "interrupted",
                    "ended_at": now,
                    "updated_at": now,
                    "error": (
                        "Plugin task execution stopped; outcome is unknown "
                        "and occurrence was not replayed"
                    ),
                }
            )
        else:
            settled = job.result().model_copy(
                update={"cancel_requested": execution.cancel_requested}
            )
        await self.tasks.record_execution(settled)
        self._plugin_jobs.pop(execution.execution_id, None)

    async def interrupt(self, task: TaskRecord) -> None:
        execution = await self.tasks.request_interrupt(task)
        if execution is None:
            return
        job = self._plugin_jobs.get(execution.execution_id)
        if job is not None:
            job.cancel()
        elif isinstance(execution.target, AgentTaskRun):
            await self._interrupt_agent(execution, reason="Task interrupt requested")
        self.tasks.changed.set()

    async def _interrupt_agent(self, execution: TaskExecution, *, reason: str) -> None:
        assert isinstance(execution.target, AgentTaskRun)
        handle = self._handles.get(execution.execution_id)
        if handle is not None:
            await self.supervisor.abort(handle, reason=reason)
            return
        with suppress(FileNotFoundError):
            run = await run_blocking(
                self.runtime.store.load_run,
                execution.target.session_id,
                execution.target.run_id,
            )
            if run.status not in {"completed", "failed", "aborted"}:
                await self.runtime.abort_run(
                    session_id=run.session_id,
                    run_id=run.run_id,
                    reason=reason,
                )

    async def stop_plugin_tasks(self, *, reconcile: bool = True) -> None:
        jobs = tuple(self._plugin_jobs.values())
        if not jobs:
            return
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        if reconcile:
            await self.reconcile()

    async def close(self, *, reconcile: bool = True) -> None:
        await self._registration.close()
        await self.stop_plugin_tasks(reconcile=reconcile)
        # Supervisor settles execution first. Receipt watchers then persist the
        # final result and release process-local data, including for parked Runs.
        await asyncio.gather(*self._waiters.values())
        if reconcile:
            await self.reconcile()
