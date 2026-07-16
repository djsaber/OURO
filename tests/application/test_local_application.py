from __future__ import annotations

import pytest

from ouro.core.errors import ConfigError
from ouro.deployment import resolve_session_backend
from ouro.local_application import LocalApplication, build_local_services
from ouro.sessions import SQLiteSessionStore


async def test_local_application_composes_and_closes_sqlite_runtime(tmp_path) -> None:
    application = LocalApplication.build(tmp_path, session_backend="sqlite")

    async with application:
        result = await application.runtime.run("hello")
        assert isinstance(application.runtime.store, SQLiteSessionStore)
        assert application.runtime.environment.home == tmp_path.resolve()
        assert application.runtime.environment.session_data_dir is None

    assert result.run.status == "completed"
    assert (tmp_path / "sessions.sqlite3").is_file()


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
