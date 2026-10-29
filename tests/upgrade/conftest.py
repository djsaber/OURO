from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def historical_home(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    backend = getattr(request, "param", "files")
    home = tmp_path / "home"
    shutil.copytree(FIXTURES / backend, home)
    for source in home.rglob("*.sql"):
        with closing(sqlite3.connect(source.with_suffix(".sqlite3"))) as connection:
            connection.executescript(source.read_text("utf-8"))
        source.unlink()

    return home


@pytest.fixture(params=["files", "sqlite"])
def native_home(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Path, str]:
    backend = request.param
    home = tmp_path / "home"
    shutil.copytree(FIXTURES / "native" / backend, home)
    source = home / "session.sql"
    database = (
        home / "sessions/ses_fixture/session-data.sqlite3"
        if backend == "files"
        else home / "sessions.sqlite3"
    )
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(source.read_text("utf-8"))
    source.unlink()
    return home, backend
