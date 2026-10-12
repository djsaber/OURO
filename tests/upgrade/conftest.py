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
