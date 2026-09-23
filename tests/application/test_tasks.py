from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime, timedelta

import pytest

from ouro.application.runtime import RuntimeService
from ouro.application.supervisor import RuntimeSupervisor
from ouro.application.task_execution import TaskExecutionCoordinator
from ouro.application.task_scheduler import TaskScheduler
from ouro.application.tasks import TaskService
from ouro.automation.local_files import LocalTaskRepository
from ouro.automation.models import TaskConfiguration, TaskConflictError, TaskRecord, TaskSchedule
from ouro.automation.sqlite import SQLiteTaskRepository
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import ConfigError, PersistenceError
from ouro.core.model_selection import DEFAULT_MODEL
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.state import RunRecord
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry, builtin_tools

NOW = datetime(2026, 9, 7, 9, tzinfo=UTC)


@asynccontextmanager
async def task_runtime(home, *, provider=None, backend="files", task_backend="files"):
    store = (
        LocalSessionStore(home)
        if backend == "files"
        else SQLiteSessionStore(home / "sessions.sqlite3")
    )
    store.initialize()
    tools = ToolRegistry()
    for tool in builtin_tools():
        tools.register(tool)
    runtime = RuntimeService(
        store=store,
        tools=tools,
        default_workspace=home,
        adapter=RuntimeAdapter(
            provider or FauxProvider(), ModelMetadata(model_id="task", provider_id="faux")
        ),
    )
    supervisor = RuntimeSupervisor(runtime)
    repository = (
        LocalTaskRepository(home) if task_backend == "files" else SQLiteTaskRepository(home)
    )
    repository.initialize()
    now = [NOW]
    tasks = TaskService(repository, runtime, clock=lambda: now[0])
    execution = TaskExecutionCoordinator(tasks, supervisor)
    await supervisor.start()
    try:
        yield tasks, execution, supervisor, now
    finally:
        await supervisor.shutdown()
        await execution.close()
        repository.release_scheduler()


def configuration(home, **changes) -> TaskConfiguration:
    return TaskConfiguration(
        workspace=str(home),
        model="faux/task",
        description="Run the scheduled instruction",
        schedule=TaskSchedule(frequency="hourly", starts_at=NOW),
        **changes,
    )


async def settle(tasks, execution, task_id, *, status="completed"):
    async with asyncio.timeout(10):
        while True:
            await execution.reconcile()
            task = await tasks.get(task_id)
            entries = await tasks.executions(task_id)
            current = next(
                (entry for entry in entries if entry.execution_id == task.last_execution_id), None
            )
            if current is not None and current.status == status:
                return task
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("task_backend", ["files", "sqlite"])
async def test_due_task_runs_with_persisted_configuration_through_shared_runtime(
    tmp_path, backend, task_backend
):
    async with task_runtime(tmp_path, backend=backend, task_backend=task_backend) as (
        tasks,
        execution,
        supervisor,
        now,
    ):
        task = await tasks.create(configuration(tmp_path, title="Scheduled report"))
        assert (await tasks.counts()).waiting == 1
        await execution.tick()
        completed = await settle(tasks, execution, task.task_id)
        entries = await tasks.executions(task.task_id)
        assert len(entries) == 1
        session = tasks.runtime.store.load_session(completed.session_id)
        assert session.session.namespace == "task"
        assert session.session.workspace == str(tmp_path)
        assert session.session.title == "Scheduled report"
        assert session.session.approval_mode == "auto"
        assert session.session.interaction_mode == "unattended"
        assert session.session.current_model == "faux/task"
        assert session.runs[0].run_id == entries[0].run_id
        assert session.runs[0].origin == "host"
        assert any(
            message.text_content == task.configuration.description for message in session.messages
        )
        counts = await tasks.counts()
        assert completed.status == "waiting"
        assert counts.waiting == 1
        assert counts.running == counts.completed == 0
        assert counts.completed_executions == 1
        assert completed.next_run_at == NOW + timedelta(hours=1)
        await execution.tick()
        assert len(await tasks.executions(task.task_id)) == 1
        assert not execution._handles
        assert not supervisor._completion_events


async def test_default_task_model_is_resolved_at_dispatch_without_freezing_configuration(
    tmp_path, monkeypatch
) -> None:
    async with task_runtime(tmp_path) as (tasks, execution, _, _):
        config = configuration(tmp_path).model_copy(update={"model": DEFAULT_MODEL})
        with pytest.raises(ConfigError, match="system default endpoint is not configured"):
            await tasks.create(config)

        monkeypatch.setattr(
            tasks.runtime, "configured_default_model", lambda: tasks.runtime.current_model()
        )
        task = await tasks.create(config)
        assert task.configuration.model == DEFAULT_MODEL
        await execution.tick()
        completed = await settle(tasks, execution, task.task_id)
        session = tasks.runtime.store.load_session(completed.session_id)
        assert session.session.current_model == "faux/task"


@pytest.mark.parametrize("mode", ["new", "reuse"])
async def test_session_creation_mode_and_deleted_reusable_session(tmp_path, mode):
    async with task_runtime(tmp_path) as (tasks, execution, _, now):
        task = await tasks.create(configuration(tmp_path, session_mode=mode))
        await execution.tick()
        first = await settle(tasks, execution, task.task_id)
        now[0] += timedelta(hours=1)
        await execution.tick()
        second = await settle(tasks, execution, task.task_id)
        assert (first.session_id == second.session_id) == (mode == "reuse")
        tasks.runtime.sessions.trash(second.session_id)
        now[0] += timedelta(hours=1)
        await execution.tick()
        third = await settle(tasks, execution, task.task_id)
        assert third.session_id != second.session_id
        assert len(await tasks.executions(task.task_id)) == 3
        counts = await tasks.counts()
        assert counts.waiting == 1
        assert counts.completed == 0
        assert counts.completed_executions == 3
        assert counts.failed_executions == 0


async def test_task_crud_validates_and_detects_concurrent_edits(tmp_path):
    async with task_runtime(tmp_path) as (tasks, execution, _, now):
        task = await tasks.create(configuration(tmp_path))
        disabled = await tasks.set_enabled(task.task_id, False, expected_revision=task.revision)
        assert disabled.status == "paused"
        assert (await tasks.counts()).awaiting_approval == 0
        now[0] += timedelta(days=3)
        await execution.tick()
        assert not await tasks.executions(task.task_id)
        with pytest.raises(TaskConflictError):
            await tasks.update(task.task_id, task.configuration, expected_revision=task.revision)
        enabled = await tasks.set_enabled(task.task_id, True, expected_revision=disabled.revision)
        assert enabled.next_run_at >= now[0]
        with pytest.raises(ValueError, match="absolute directory"):
            await tasks.create(
                task.configuration.model_copy(update={"workspace": "missing-relative"})
            )
        with pytest.raises(ConfigError, match="not found"):
            await tasks.create(task.configuration.model_copy(update={"model": "missing/model"}))
        await tasks.delete(task.task_id, expected_revision=enabled.revision)
        assert not await tasks.list()


async def test_missed_occurrences_coalesce_and_claim_is_atomic(tmp_path):
    async with task_runtime(tmp_path) as (tasks, execution, _, now):
        task = await tasks.create(configuration(tmp_path))
        now[0] += timedelta(days=1000)
        results = await asyncio.gather(
            tasks.claim(task, "ses_a"),
            tasks.claim(task, "ses_b"),
            return_exceptions=True,
        )
        assert sum(isinstance(result, TaskConflictError) for result in results) == 1
        claimed = await tasks.get(task.task_id)
        assert claimed.next_run_at == now[0] + timedelta(hours=1)
        assert len(await tasks.executions(task.task_id)) == 1
        # A crash between claim and Session creation does not replay the prompt.
        await execution.recover()
        assert (await tasks.get(task.task_id)).status == "waiting"
        assert (await tasks.executions(task.task_id))[0].status == "interrupted"
        assert not tasks.runtime.sessions.catalog().entries


async def test_scheduler_election_and_startup_dispatch(tmp_path):
    async with task_runtime(tmp_path) as (tasks, execution, _, now):
        task = await tasks.create(configuration(tmp_path))
        peer = SQLiteTaskRepository(tmp_path)
        assert tasks.repository.acquire_scheduler()
        assert not peer.acquire_scheduler()
        tasks.repository.release_scheduler()
        assert peer.acquire_scheduler()
        scheduler = TaskScheduler(tasks, execution, refresh_seconds=0.02)
        await scheduler.start()
        try:
            await asyncio.sleep(0.06)
            assert not await tasks.executions(task.task_id)
            peer.release_scheduler()
            async with asyncio.timeout(10):
                while (await tasks.counts()).completed_executions != 1:
                    await asyncio.sleep(0.01)
            assert len(await tasks.executions(task.task_id)) == 1
        finally:
            await scheduler.stop()


async def test_approval_pauses_until_user_resumes_the_same_run(tmp_path):
    target = tmp_path / "approved.txt"
    provider = FauxProvider(
        event_batches=[
            [
                ProviderStreamEvent.from_tool_call(
                    {
                        "id": "write",
                        "name": "Write",
                        "arguments": {"path": str(target), "content": "approved"},
                    }
                ),
                ProviderStreamEvent(type="done"),
            ],
            [ProviderStreamEvent(type="text_delta", text="Done"), ProviderStreamEvent(type="done")],
        ]
    )
    async with task_runtime(tmp_path, provider=provider) as (tasks, execution, _, now):
        task = await tasks.create(configuration(tmp_path, approval_mode="approval"))
        await execution.tick()
        paused = await settle(tasks, execution, task.task_id, status="paused")
        assert (await tasks.counts()).awaiting_approval == 1
        assert not target.exists()
        with pytest.raises(TaskConflictError):
            await tasks.delete(task.task_id, expected_revision=paused.revision)
        now[0] += timedelta(hours=3)
        await execution.tick()
        assert len(await tasks.executions(task.task_id)) == 1
        record = (await tasks.executions(task.task_id))[0]
        run = tasks.runtime.store.load_run(record.session_id, record.run_id)
        result = await tasks.runtime.approve_tool(
            session_id=record.session_id,
            run_id=record.run_id,
            approval_id=run.pending_approval_id,
            approved=True,
        )
        assert result.run.run_id == record.run_id
        await settle(tasks, execution, task.task_id)
        assert target.read_text() == "approved"
        assert (await tasks.counts()).awaiting_approval == 0


class SelectivelyBlockingProvider:
    provider_id = "faux"

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, *, model, messages, system_prompt="", tools=None, request_context=None):
        if "scheduled" in messages[-1].text_content:
            self.started.set()
            await self.release.wait()
        yield ProviderStreamEvent(type="text_delta", text="reply")
        yield ProviderStreamEvent(type="done")


async def test_background_concurrency_preserves_interactive_execution_and_abort(tmp_path):
    provider = SelectivelyBlockingProvider()
    async with task_runtime(tmp_path, provider=provider) as (tasks, execution, supervisor, now):
        first = await tasks.create(configuration(tmp_path, title="First"))
        second = await tasks.create(configuration(tmp_path, title="Second"))
        await execution.tick()
        async with asyncio.timeout(5):
            await provider.started.wait()
        await execution.tick()
        assert (
            len(await tasks.executions(first.task_id)) + len(await tasks.executions(second.task_id))
            == 1
        )
        foreground = await supervisor.submit("Interactive question")
        async with asyncio.timeout(5):
            assert (await supervisor.wait(foreground)).status == "completed"
        supervisor.release_handle(foreground)
        active = tasks.repository.active()
        await supervisor.abort(active[0].session_id)
        await settle(tasks, execution, active[0].task_id, status="interrupted")
        provider.release.set()


async def test_launch_failure_is_visible_and_does_not_block_other_tasks(tmp_path):
    async with task_runtime(tmp_path) as (tasks, execution, _, now):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        invalidated = await tasks.create(configuration(workspace))
        valid = await tasks.create(configuration(tmp_path))
        workspace.rmdir()
        await execution.tick()
        await settle(tasks, execution, valid.task_id)
        await execution.tick()
        counts = await tasks.counts()
        assert counts.completed_executions == 1
        assert counts.failed_executions == 1
        failed = await tasks.get(invalidated.task_id)
        assert failed.status == "waiting"
        assert counts.waiting == 2
        assert (await tasks.executions(failed.task_id))[0].status == "failed"
        assert "absolute directory" in failed.error
        await settle(tasks, execution, valid.task_id)


async def test_expired_plan_does_not_dispatch_after_restart(tmp_path):
    async with task_runtime(tmp_path) as (tasks, execution, _, now):
        config = configuration(tmp_path).model_copy(
            update={"schedule": TaskSchedule(frequency="hourly", starts_at=NOW, ends_at=NOW)}
        )
        task = await tasks.create(config)
        now[0] += timedelta(minutes=1)
        await execution.recover()
        await execution.tick()
        expired = await tasks.get(task.task_id)
        assert expired.next_run_at is None
        assert expired.status == "completed"
        assert not await tasks.executions(task.task_id)
        assert not tasks.runtime.sessions.catalog().entries
        assert (await tasks.counts()).completed_executions == 0


@pytest.mark.parametrize("outcome", ["completed", "failed", "interrupted"])
@pytest.mark.parametrize("plan", ["repeating", "disabled", "last_occurrence", "ends_during_run"])
async def test_execution_result_is_separate_from_remaining_schedule(tmp_path, outcome, plan):
    async with task_runtime(tmp_path) as (tasks, _, _, now):
        config = configuration(tmp_path)
        if plan in {"last_occurrence", "ends_during_run"}:
            end = NOW if plan == "last_occurrence" else NOW + timedelta(hours=1)
            config = config.model_copy(
                update={"schedule": config.schedule.model_copy(update={"ends_at": end})}
            )
        task = await tasks.create(config)
        receipt = await tasks.claim(task, "ses_scheduled")
        assert receipt is not None
        task = await tasks.get(task.task_id)
        if plan == "disabled":
            await tasks.set_enabled(task.task_id, False, expected_revision=task.revision)
        if plan == "ends_during_run":
            now[0] += timedelta(hours=2)
        await tasks.record_execution(
            receipt.model_copy(update={"status": outcome, "ended_at": now[0], "updated_at": now[0]})
        )
        task = await tasks.get(task.task_id)
        assert task.active_execution_id is None
        assert task.status == (
            "waiting" if plan == "repeating" else "paused" if plan == "disabled" else outcome
        )
        if plan in {"last_occurrence", "ends_during_run"}:
            assert task.next_run_at is None
        entries = await tasks.executions(task.task_id)
        assert len(entries) == 1 and entries[0].status == outcome
        counts = await tasks.counts()
        assert counts.waiting == (plan == "repeating")
        assert counts.completed_executions == (outcome == "completed")
        assert counts.failed_executions == (outcome == "failed")


async def test_extending_an_ended_schedule_returns_task_to_waiting(tmp_path):
    async with task_runtime(tmp_path) as (tasks, execution, _, now):
        config = configuration(tmp_path)
        config = config.model_copy(
            update={"schedule": config.schedule.model_copy(update={"ends_at": NOW})}
        )
        task = await tasks.create(config)
        await execution.tick()
        task = await settle(tasks, execution, task.task_id)
        assert task.status == "completed" and task.next_run_at is None
        now[0] += timedelta(minutes=1)
        task = await tasks.update(
            task.task_id, configuration(tmp_path), expected_revision=task.revision
        )
        assert task.status == "waiting"
        assert task.next_run_at == NOW + timedelta(hours=1)
        assert (await tasks.counts()).completed_executions == 1


async def test_restart_preserves_pending_approval_and_exact_run_identity(tmp_path):
    target = tmp_path / "restart.txt"
    provider = FauxProvider(
        event_batches=[
            [
                ProviderStreamEvent.from_tool_call(
                    {
                        "id": "write",
                        "name": "Write",
                        "arguments": {"path": str(target), "content": "approved after restart"},
                    }
                ),
                ProviderStreamEvent(type="done"),
            ]
        ]
    )
    async with task_runtime(tmp_path, provider=provider) as (tasks, execution, _, now):
        task = await tasks.create(configuration(tmp_path, approval_mode="approval"))
        await execution.tick()
        await settle(tasks, execution, task.task_id, status="paused")
        receipt = (await tasks.executions(task.task_id))[0]

    async with task_runtime(tmp_path) as (tasks, execution, supervisor, now):
        await execution.recover()
        await execution.tick()
        assert (await tasks.get(task.task_id)).status == "paused"
        assert (await tasks.counts()).awaiting_approval == 1
        assert not target.exists()
        run = tasks.runtime.store.load_run(receipt.session_id, receipt.run_id)
        handle = await supervisor.resolve_tool_approval(
            session_id=receipt.session_id,
            run_id=receipt.run_id,
            approval_id=run.pending_approval_id,
            approved=True,
        )
        async with asyncio.timeout(5):
            assert (await supervisor.wait(handle)).status == "completed"
        supervisor.release_handle(handle)
        await settle(tasks, execution, task.task_id)
        assert len(await tasks.executions(task.task_id)) == 1
        assert target.read_text() == "approved after restart"


async def test_corrupt_task_storage_is_diagnosed_without_stopping_interactive_runs(tmp_path):
    async with task_runtime(tmp_path, task_backend="sqlite") as (tasks, execution, supervisor, now):
        task = await tasks.create(configuration(tmp_path))
        with closing(sqlite3.connect(tasks.repository.path)) as connection, connection:
            connection.execute(
                "UPDATE tasks SET payload=? WHERE task_id=?",
                ('{"configuration":"private instruction"}', task.task_id),
            )
        with pytest.raises(PersistenceError) as raised:
            await tasks.get(task.task_id)
        assert "private instruction" not in str(raised.value)
        scheduler = TaskScheduler(tasks, execution, refresh_seconds=0.02)
        await scheduler.start()
        try:
            async with asyncio.timeout(5):
                while not scheduler.diagnostics:
                    await asyncio.sleep(0.01)
            assert scheduler.diagnostics[0].code == "task_scheduler_failed"
            handle = await supervisor.submit("Interactive question")
            assert (await supervisor.wait(handle)).status == "completed"
            supervisor.release_handle(handle)
        finally:
            await scheduler.stop()


async def test_busy_reusable_sessions_do_not_starve_tasks_beyond_admission_window(tmp_path):
    async with task_runtime(tmp_path) as (tasks, execution, _, now):
        session = tasks.runtime.create_session(title="Busy reusable session", namespace="task")
        config = configuration(tmp_path, session_mode="reuse")
        tasks.runtime.prepare_run(
            RunRecord(session_id=session.session_id),
            model=config.model,
            reasoning=config.reasoning,
            workspace=config.workspace,
            approval_mode="auto",
            interaction_mode="interactive",
        )
        for index in range(101):
            tasks.repository.commit(
                TaskRecord(
                    task_id=f"task_busy_{index:03}",
                    configuration=config,
                    created_at=NOW,
                    updated_at=NOW,
                    next_run_at=NOW,
                    session_id=session.session_id,
                ),
                expected_revision=None,
            )
        valid = TaskRecord(
            task_id="task_valid",
            configuration=configuration(tmp_path),
            created_at=NOW,
            updated_at=NOW,
            next_run_at=NOW,
        )
        tasks.repository.commit(valid, expected_revision=None)
        await execution.tick()
        assert not await tasks.executions(valid.task_id)
        await execution.tick()
        await settle(tasks, execution, valid.task_id)
        assert (await tasks.counts()).waiting == 102
