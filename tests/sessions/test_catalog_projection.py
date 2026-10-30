from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from ouro.core.errors import PersistenceError
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from tests.resource_fixtures import resource_record
from tests.session_storage import read_session_payload, write_session_payload
from tests.sessions.test_tool_fact_storage import commit, facts


@pytest.mark.parametrize("backend", ("files", "sqlite"))
@pytest.mark.parametrize("model", (None, "unavailable-provider/unavailable-endpoint"))
def test_catalog_preserves_selected_model_route(backend, model, tmp_path):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    session = SessionRecord(current_model=model)
    store.create_session(session)
    assert store.refresh_catalog(session.session_id) is None

    for catalog in (store.read_catalog(), store.rebuild_catalog(), store.read_catalog()):
        entry = catalog.sessions[0]
        assert entry.session_id == session.session_id
        assert entry.current_model == model
        payload = entry.model_dump(mode="json")
        assert payload["current_model"] == model
        assert "current_provider" not in payload
        assert "current_endpoint" not in payload


@pytest.mark.parametrize("backend", ("files", "sqlite"))
def test_catalog_preview_does_not_materialize_call_arguments(backend, tmp_path, monkeypatch):
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    session, run, call, _, _ = facts(store)
    call = call.model_copy(
        update={
            "request": call.request.model_copy(
                update={"arguments": {"body": "large parameters " * 65536}}
            )
        }
    )
    commit(store, session, calls=(call,))
    message = MessageRecord.text(
        session_id=session.session_id,
        run_id=run.run_id,
        role="assistant",
        text="Visible progress",
    )
    message.turn_id = call.turn_id
    message.call_refs = (call.identity,)
    store.append_message(message)
    original_open = Path.open

    def no_calls(path, *args, **kwargs):
        if "calls" in path.parts:
            raise AssertionError("Catalog read original Call parameters")
        return original_open(path, *args, **kwargs)

    if isinstance(store, LocalSessionStore):
        monkeypatch.setattr(Path, "open", no_calls)
        catalog = store.scan_catalog()
    else:
        with store.session_mutation(session.session_id):
            connection = store._transaction_connection()

            def authorize(action, table, column, *args):
                if action == sqlite3.SQLITE_READ and table == "tool_calls":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            try:
                assert store.refresh_catalog(session.session_id) is None
            finally:
                connection.set_authorizer(None)
        catalog = store.read_catalog()
    assert catalog.sessions[0].last_message_preview == "Visible progress"


@pytest.mark.parametrize("backend", ("files", "sqlite"))
def test_catalog_read_does_not_validate_canonical_session_rows(
    backend: str,
    tmp_path,
) -> None:
    store = (
        LocalSessionStore(tmp_path / "files")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")
    )
    store.initialize()
    session = SessionRecord(session_id="ses_deferred_validation", title="Indexed title")
    store.create_session(session)
    assert store.refresh_catalog(session.session_id) is None

    if isinstance(store, LocalSessionStore):
        write_session_payload(store, session.session_id, "{broken")
    else:
        with closing(sqlite3.connect(store.database)) as connection:
            connection.execute(
                "UPDATE sessions SET payload = ? WHERE session_id = ?",
                ("{broken", session.session_id),
            )
            connection.commit()

    catalog = store.read_catalog()

    assert catalog.sessions[0].title == "Indexed title"
    with pytest.raises(PersistenceError):
        store.load_session_record(session.session_id)


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
    reference = resource_record(resource_id="res_" + "d" * 32)
    store.create_session_snapshot(
        session, [first, latest], [reference], resource_contents=((reference, b"fixture"),)
    )
    store.save_run(run)
    assert store.refresh_catalog(session.session_id) is None
    database = (
        store.sessions_dir / session.session_id / "session-data.sqlite3"
        if isinstance(store, LocalSessionStore)
        else store.database
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "INSERT INTO compactions(session_id, compaction_id, covered_end_message_id, "
            "history_boundary_message_id, payload) VALUES(?, ?, ?, ?, ?)",
            (session.session_id, "cmp_broken", first.message_id, latest.message_id, "{broken"),
        )

    def reject_full_history(*_args, **_kwargs):
        raise AssertionError("Catalog must not load RuntimeSessionSnapshot")

    monkeypatch.setattr(store, "load_runtime_session", reject_full_history)
    catalog = store.read_catalog()

    assert len(catalog.sessions) == 1
    entry = catalog.sessions[0]
    assert entry.message_count == 2
    assert entry.run_count == 1
    assert entry.resource_count == 1
    assert entry.health == "ok"
    assert entry.last_message_preview == "bounded latest preview"
    assert "historical-needle" not in catalog.model_dump_json()


@pytest.mark.parametrize("backend", ("files", "sqlite"))
def test_catalog_projects_primary_and_subagent_session_identity(
    backend: str,
    tmp_path,
) -> None:
    store = (
        LocalSessionStore(tmp_path / "files")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")
    )
    store.initialize()
    root = SessionRecord(session_id="ses_catalog_root", title="Root")
    child = SessionRecord(
        session_id="ses_catalog_child",
        title="Child",
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id="dlg_catalog_child",
    )
    store.create_session(root)
    store.create_session(child)
    assert store.refresh_catalog(root.session_id) is None
    assert store.refresh_catalog(child.session_id) is None

    entries = {entry.session_id: entry for entry in store.read_catalog().sessions}

    assert entries[root.session_id].session_kind == "primary"
    assert entries[child.session_id].session_kind == "subagent"


@pytest.mark.parametrize("backend", ("files", "sqlite"))
def test_corrupt_catalog_projection_preserves_readable_subagent_identity(
    backend: str,
    tmp_path,
) -> None:
    store = (
        LocalSessionStore(tmp_path / "files")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")
    )
    store.initialize()
    child = SessionRecord(
        session_id="ses_corrupt_child",
        title="Corrupt child",
        session_kind="subagent",
        root_session_id="ses_root",
        parent_session_id="ses_root",
        delegation_id="dlg_corrupt_child",
    )
    store.create_session(child)
    if isinstance(store, LocalSessionStore):
        payload = json.loads(read_session_payload(store, child.session_id))
        payload.pop("delegation_id")
        write_session_payload(store, child.session_id, json.dumps(payload))
    else:
        with closing(sqlite3.connect(store.database)) as connection:
            row = connection.execute(
                "SELECT payload FROM sessions WHERE session_id = ?",
                (child.session_id,),
            ).fetchone()
            assert row is not None
            payload = json.loads(row[0])
            payload.pop("delegation_id")
            connection.execute(
                "UPDATE sessions SET payload = ? WHERE session_id = ?",
                (json.dumps(payload), child.session_id),
            )
            connection.commit()

    entry = store.rebuild_catalog().sessions[0]

    assert entry.session_id == child.session_id
    assert entry.session_kind == "subagent"
    assert entry.health == "corrupt"
    assert entry.error_detail is not None


@pytest.mark.parametrize("backend", ("files", "sqlite"))
def test_catalog_read_isolates_one_invalid_projection_entry(
    backend: str,
    tmp_path,
) -> None:
    store = (
        LocalSessionStore(tmp_path / "files")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")
    )
    store.initialize()
    valid = SessionRecord(session_id="ses_catalog_valid", title="Valid")
    invalid = SessionRecord(session_id="ses_catalog_invalid", title="Invalid")
    store.create_session(valid)
    store.create_session(invalid)
    assert store.refresh_catalog(valid.session_id) is None
    assert store.refresh_catalog(invalid.session_id) is None

    if isinstance(store, LocalSessionStore):
        index_path = store._catalog.index_path
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        raw_entry = next(
            item for item in payload["sessions"] if item["session_id"] == invalid.session_id
        )
        raw_entry.pop("session_kind")
        index_path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        with closing(sqlite3.connect(store.database)) as connection:
            row = connection.execute(
                "SELECT payload FROM session_catalog WHERE session_id = ?",
                (invalid.session_id,),
            ).fetchone()
            assert row is not None
            raw_entry = json.loads(row[0])
            raw_entry.pop("session_kind")
            connection.execute(
                "UPDATE session_catalog SET payload = ? WHERE session_id = ?",
                (json.dumps(raw_entry), invalid.session_id),
            )
            connection.commit()

    entries = {entry.session_id: entry for entry in store.read_catalog().sessions}

    assert entries[valid.session_id].health == "ok"
    assert entries[invalid.session_id].title == "Invalid"
    assert entries[invalid.session_id].session_kind is None
    assert entries[invalid.session_id].health is None
    assert entries[invalid.session_id].error_detail is None
    assert entries[invalid.session_id].catalog_error is not None
    assert "session_kind" in entries[invalid.session_id].catalog_error
    assert store.load_session(invalid.session_id).session == invalid
    assert store.load_session_observation(invalid.session_id).session == invalid


def test_sqlite_catalog_read_uses_only_projection_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "ouro.db")
    store.initialize()
    for index in range(25):
        session = SessionRecord(session_id=f"ses_{index}", title=f"Session {index}")
        store.create_session(session)
        assert store.refresh_catalog(session.session_id) is None

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
        if statement.lstrip().upper().startswith("SELECT PAYLOAD, SESSION_ID FROM SESSION_CATALOG")
    ]
    canonical_selects = [
        statement for statement in statements if "FROM SESSIONS S" in statement.upper()
    ]
    assert len(catalog.sessions) == 25
    assert len(catalog_selects) == 1
    assert "ORDER BY" not in catalog_selects[0].upper()
    assert canonical_selects == []
