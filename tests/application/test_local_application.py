from __future__ import annotations

import pytest

from ouro.core.errors import ConfigError
from ouro.core.state import SessionRecord
from ouro.deployment import resolve_session_backend
from ouro.local_application import (
    LocalApplication,
    build_local_services,
    open_local_agent_definitions,
)
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.local_index import LocalSessionIndex


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


async def test_local_agent_definition_catalog_includes_active_plugin_tools(tmp_path) -> None:
    application = LocalApplication.build(tmp_path)
    await application.start()
    try:
        assert application.plugins is not None
        assert (await application.plugins.enable("dev.ouro.toolkit")).status == "active"
    finally:
        await application.close()

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
