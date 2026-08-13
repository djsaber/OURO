from __future__ import annotations

import pytest

from ouro.core.errors import ConfigError
from ouro.deployment import resolve_session_backend
from ouro.local_application import (
    LocalApplication,
    build_local_services,
    open_local_agent_definitions,
)
from ouro.sessions import SQLiteSessionStore


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
