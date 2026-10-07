from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from ouro.core.errors import UnsupportedDataVersionError
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.sessions.layout import DATA_LAYOUT_VERSION
from ouro.sessions.sqlite import SQLITE_SCHEMA_VERSION


@pytest.mark.parametrize("version", [None, 2, 3, 4, 5, 6, 7, 8, DATA_LAYOUT_VERSION + 1])
def test_file_store_rejects_other_formats_without_reading_or_rewriting_records(tmp_path, version):
    session_dir = tmp_path / "sessions" / "ses_unsupported"
    session_dir.mkdir(parents=True)
    messages = session_dir / "messages.jsonl"
    messages.write_text('{"role":"system_synthetic"}\n', encoding="utf-8")
    if version is not None:
        (tmp_path / "layout.json").write_text(
            json.dumps({"data_layout_version": version}), encoding="utf-8"
        )
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    with pytest.raises(UnsupportedDataVersionError):
        LocalSessionStore(tmp_path).initialize()

    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.parametrize("version", [None, 1, 2, 3, 4, 5, 6, SQLITE_SCHEMA_VERSION + 1])
def test_sqlite_store_rejects_other_formats_before_creating_current_tables(tmp_path, version):
    database = tmp_path / "sessions.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if version is not None:
            connection.execute(
                "INSERT INTO metadata VALUES ('sqlite_schema_version', ?)", (str(version),)
            )
    before = database.read_bytes()

    with pytest.raises(UnsupportedDataVersionError):
        SQLiteSessionStore(database).initialize()

    assert database.read_bytes() == before
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("metadata",)]


def test_sqlite_store_rejects_existing_tables_without_metadata(tmp_path):
    database = tmp_path / "sessions.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE sessions (payload TEXT)")
    before = database.read_bytes()

    with pytest.raises(UnsupportedDataVersionError, match="no format version"):
        SQLiteSessionStore(database).initialize()

    assert database.read_bytes() == before


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_current_store_reopens_without_converting_data(tmp_path, backend):
    def make_store():
        return (
            LocalSessionStore(tmp_path)
            if backend == "files"
            else SQLiteSessionStore(tmp_path / "sessions.sqlite3")
        )

    store = make_store()
    store.initialize()
    session = SessionRecord(title="Current format")
    store.create_session(session)
    before = store.load_session_record(session.session_id)
    reopened = make_store()
    reopened.initialize()
    assert reopened.load_session_record(session.session_id) == before
    if backend == "files":
        assert (
            json.loads((tmp_path / "layout.json").read_text())["data_layout_version"]
            == DATA_LAYOUT_VERSION
        )
    else:
        with closing(sqlite3.connect(tmp_path / "sessions.sqlite3")) as connection:
            assert connection.execute(
                "SELECT value FROM metadata WHERE key='sqlite_schema_version'"
            ).fetchone() == (str(SQLITE_SCHEMA_VERSION),)
