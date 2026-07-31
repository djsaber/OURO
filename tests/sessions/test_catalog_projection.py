from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from ouro.core.messages import MessageRecord
from ouro.core.resources import SessionResourceReference
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


@pytest.mark.parametrize("backend", ("files", "sqlite"))
def test_catalog_uses_bounded_projection_without_loading_runtime_history(
    backend: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    store = (
        LocalSessionStore(tmp_path / "files")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")
    )
    store.initialize()
    first = MessageRecord.text(
        session_id="ses_catalog",
        role="user",
        text="historical-needle-" + "x" * 200_000,
        sequence=1,
    )
    latest = MessageRecord.text(
        session_id="ses_catalog",
        role="assistant",
        text="bounded latest preview",
        sequence=2,
    )
    run = RunRecord(session_id="ses_catalog", status="completed")
    session = SessionRecord(
        session_id="ses_catalog",
        message_count=2,
        last_message_id=latest.message_id,
        last_run_id=run.run_id,
    )
    reference = SessionResourceReference(resource_id="res_" + "d" * 64)
    store.create_session_snapshot(session, [first, latest], [reference])
    store.save_run(run)
    if isinstance(store, LocalSessionStore):
        (store.sessions_dir / session.session_id / "compactions.jsonl").write_text(
            "{broken\n",
            encoding="utf-8",
        )
    else:
        with closing(sqlite3.connect(store.database)) as connection:
            connection.execute(
                "INSERT INTO compactions(session_id, compaction_id, payload) VALUES(?, ?, ?)",
                (session.session_id, "cmp_broken", "{broken"),
            )
            connection.commit()

    def reject_full_history(*_args, **_kwargs):
        raise AssertionError("Catalog must not load RuntimeSessionSnapshot")

    monkeypatch.setattr(store, "load_runtime_session", reject_full_history)
    catalog = (
        store.rebuild_catalog() if isinstance(store, LocalSessionStore) else store.read_catalog()
    )

    assert len(catalog.sessions) == 1
    entry = catalog.sessions[0]
    assert entry.message_count == 2
    assert entry.run_count == 1
    assert entry.resource_count == 1
    assert entry.last_message_preview == "bounded latest preview"
    assert "historical-needle" not in catalog.model_dump_json()


def test_sqlite_catalog_rebuild_is_one_bounded_aggregate_query(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "ouro.db")
    store.initialize()
    for index in range(25):
        session = SessionRecord(session_id=f"ses_{index}", title=f"Session {index}")
        store.create_session(session)

    statements: list[str] = []
    original_connect = store._connect

    def traced_connect() -> sqlite3.Connection:
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store, "_connect", traced_connect)

    catalog = store.read_catalog()

    catalog_selects = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT S.SESSION_ID")
    ]
    assert len(catalog.sessions) == 25
    assert len(catalog_selects) == 1
