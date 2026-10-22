from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ouro.automation.local_files import LocalTaskRepository
from ouro.automation.sqlite import SQLiteTaskRepository
from ouro.core.errors import ConfigError
from ouro.core.state import SessionRecord
from ouro.core.tasks import AgentTaskTarget, TaskConfiguration, TaskSchedule
from ouro.deployment import resolve_session_backend, resolve_task_backend
from ouro.local_application import (
    LocalApplication,
    build_local_services,
    open_local_agent_definitions,
)
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.local_index import LocalSessionIndex


async def test_local_application_discovers_skills_after_startup(tmp_path):
    async with LocalApplication.build(tmp_path) as application:
        assert await application.available_skills() == ()
        skill = tmp_path / "skills" / "writer" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: writer\ndescription: Write text.\n---\nWrite.\n", encoding="utf-8"
        )
        assert [item.name for item in await application.available_skills()] == ["writer"]
        previous = application.plugin_runtime.catalog()
        assert [item.name for item in await application.available_skills()] == ["writer"]
        assert application.plugin_runtime.catalog() is previous
        skill.unlink()
        assert await application.available_skills() == ()


async def test_application_close_retries_failed_plugin_cleanup(tmp_path, monkeypatch):
    application = LocalApplication.build(tmp_path)
    await application.start()
    assert application.plugin_runtime is not None
    plugin_ids = [item.plugin_id for item in application.plugin_runtime.catalog().generations]
    close_plugins = application.plugin_runtime.close
    attempts = 0

    async def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("plugin resource cleanup failed")
        await close_plugins()

    monkeypatch.setattr(application.plugin_runtime, "close", fail_once)
    try:
        with pytest.raises(ExceptionGroup, match="shutdown failed"):
            await application.close()
        await application.close()
        assert attempts == 2
        assert not any(application.plugin_runtime.has_resources(item) for item in plugin_ids)
    finally:
        await close_plugins()


@pytest.mark.parametrize("task_backend", ["files", "sqlite"])
@pytest.mark.parametrize("session_backend", ["files", "sqlite"])
async def test_task_sessions_use_the_composed_session_backend(
    tmp_path, task_backend, session_backend, monkeypatch
):
    application = LocalApplication.build(
        tmp_path, task_backend=task_backend, session_backend=session_backend
    )
    async with application:
        tasks = application.tasks
        assert tasks is not None
        assert type(tasks.repository) is (
            LocalTaskRepository if task_backend == "files" else SQLiteTaskRepository
        )
        assert tasks.runtime is application.runtime
        now = datetime.now(UTC)
        monkeypatch.setattr(tasks, "clock", lambda: now)
        task = await tasks.create(
            TaskConfiguration(
                schedule=TaskSchedule(frequency="custom", unit="minutes", starts_at=now),
                target=AgentTaskTarget(
                    prompt="Run this scheduled instruction",
                    workspace=str(tmp_path),
                    model=application.runtime.current_model().key,
                ),
            )
        )
        async with asyncio.timeout(10):
            while (await tasks.counts()).completed_executions != 1:
                await asyncio.sleep(0.01)
        task = await tasks.get(task.task_id)
        session = application.runtime.store.load_session(task.session_id)
        assert session.session.namespace == "task"
        assert session.session.session_id == task.session_id
        assert session.runs[0].status == "completed"
        assert (tmp_path / "sessions.sqlite3").exists() == (session_backend == "sqlite")
        if session_backend == "files":
            assert (tmp_path / "sessions" / task.session_id / "session.json").is_file()
        assert (tmp_path / "tasks.sqlite3").exists() == (task_backend == "sqlite")
        assert (tmp_path / "tasks" / "layout.json").exists() == (task_backend == "files")


def test_task_backend_defaults_and_override_are_independent(monkeypatch):
    monkeypatch.delenv("OURO_TASK_BACKEND", raising=False)
    monkeypatch.setenv("OURO_SESSION_BACKEND", "sqlite")
    assert resolve_task_backend() == "files"
    monkeypatch.setenv("OURO_TASK_BACKEND", "sqlite")
    assert resolve_task_backend() == "sqlite"
    assert resolve_task_backend("files") == "files"
    assert resolve_session_backend() == "sqlite"
    monkeypatch.setenv("OURO_TASK_BACKEND", "remote")
    with pytest.raises(ConfigError, match="OURO_TASK_BACKEND"):
        resolve_task_backend()


async def test_local_application_composes_and_closes_sqlite_runtime(tmp_path) -> None:
    application = LocalApplication.build(tmp_path, session_backend="sqlite")

    async with application:
        result = await application.runtime.run("hello")
        assert isinstance(application.runtime.store, SQLiteSessionStore)
        assert application.runtime.environment.home == tmp_path.resolve()
        assert application.runtime.environment.session_data_dir is None
        assert application.runtime.environment.artifact_root == tmp_path.resolve() / "artifacts"
        assert application.agents is not None
        assert application.agent_definitions is not None
        assert [
            item.agent_id
            for item in application.agent_definitions.list(
                tools=application.runtime.tools
            ).definitions
        ] == ["general"]

    assert result.run.status == "completed"
    assert (tmp_path / "sessions.sqlite3").is_file()


async def test_local_agent_definition_catalog_includes_builtin_tools(tmp_path) -> None:
    async with open_local_agent_definitions(tmp_path) as services:
        assert {"Glob", "Grep", "TodoList"} <= set(services.tools.names())


def test_local_services_expose_complete_host_facets_with_sqlite(tmp_path) -> None:
    services = build_local_services(tmp_path, session_backend="sqlite")

    created = services.sessions.create(title="Facets")

    assert services.sessions.open(created.session.session_id).session.title == "Facets"
    assert services.resources.list_resources(created.session.session_id).resources == ()
    assert services.usage.list_entries().entries == ()
    assert services.configuration.overview().mcp.servers == []


def test_environment_selects_backend_and_explicit_value_has_precedence(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OURO_SESSION_BACKEND", "sqlite")
    sqlite_services = build_local_services(tmp_path)
    sqlite_session = sqlite_services.sessions.create(title="SQLite").session

    file_services = build_local_services(tmp_path, session_backend="files")

    assert (tmp_path / "sessions.sqlite3").is_file()
    assert sqlite_services.sessions.open(sqlite_session.session_id).session.title == "SQLite"
    assert file_services.sessions.catalog().entries == ()


def test_invalid_environment_backend_fails_at_composition_boundary(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OURO_SESSION_BACKEND", "remote")

    with pytest.raises(ConfigError, match="OURO_SESSION_BACKEND"):
        resolve_session_backend()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("files", "sqlite"))
async def test_application_start_does_not_read_session_history(
    tmp_path,
    monkeypatch,
    backend: str,
) -> None:
    seed = LocalApplication.build(tmp_path, session_backend=backend)
    seed.runtime.store.create_session(SessionRecord(title="Historical"))

    def unexpected_history_read(*_args, **_kwargs):
        raise AssertionError("application startup must not read Session history")

    for name in (
        "list_sessions",
        "load_session_record",
        "load_runs",
        "list_delegations",
        "read_catalog",
    ):
        monkeypatch.setattr(type(seed.runtime.store), name, unexpected_history_read)
    if backend == "files":
        monkeypatch.setattr(LocalSessionIndex, "scan", unexpected_history_read)

    application = LocalApplication.build(tmp_path, session_backend=backend)
    await application.start()

    assert application.startup_report.diagnostics == ()
    await application.close()


@pytest.mark.asyncio
async def test_corrupt_historical_session_does_not_block_start_or_new_run(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    historical = SessionRecord(title="Corrupt history")
    store.create_session(historical)
    (store.sessions_dir / historical.session_id / "session.json").write_text(
        "{not-json",
        encoding="utf-8",
    )

    application = LocalApplication.build(tmp_path, session_backend="files")
    async with application:
        result = await application.runtime.run("new independent Session")

    assert result.run.status == "completed"
    assert result.session.session_id != historical.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("files", "sqlite"))
async def test_primary_fork_runs_with_missing_lineage_root(tmp_path, backend: str) -> None:
    application = LocalApplication.build(tmp_path, session_backend=backend)
    branch = SessionRecord(
        session_id="ses_surviving_branch",
        root_session_id="ses_deleted_lineage_root",
        parent_session_id="ses_deleted_lineage_root",
        title="Surviving branch",
    )
    application.runtime.store.create_session(branch)

    async with application:
        result = await application.runtime.run(
            "continue from the surviving branch",
            session_id=branch.session_id,
        )
        assert application.agents is not None
        tree = application.agents.agent_tree_snapshot(branch.session_id)

    assert branch.agent_root_session_id == branch.session_id
    assert result.run.status == "completed"
    assert tree.root_session_id == branch.session_id
