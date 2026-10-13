"""Transform private SQLite copies; never open a user's source database."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing
from functools import partial
from typing import Any
from uuid import uuid4

from ouro.billing.schema import USAGE_SCHEMA
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions.catalog import SessionCatalogProjection, catalog_entry_from_projection
from ouro_upgrade.errors import UpgradeBlockedError, UpgradeDataError, UpgradeError, require
from ouro_upgrade.records import convert, convert_task, dumps, loads, validate
from ouro_upgrade.units import UpgradeUnit, UpgradeUnits
from ouro_upgrade.workspace import Workspace

_IDENTITIES = {
    "session": ("session_id",),
    "message": ("session_id", "message_id", "sequence"),
    "event": ("session_id", "event_id", "run_id", "sequence"),
    "run": ("session_id", "run_id"),
    "compaction": ("session_id", "compaction_id"),
    "context": ("session_id", "context_id"),
    "resource": ("resource_id",),
    "harness": ("session_id", "version_id"),
    "commit": ("operation_id",),
    "delegation": ("delegation_id",),
    "task": ("task_id", "revision"),
    "execution": ("task_id", "execution_id"),
    "usage": ("entry_id",),
    "usage_event": ("event_id",),
}


def require_structure(condition: bool, location: str, reason: str) -> None:
    if not condition:
        raise UpgradeBlockedError(f"{location}: {reason}")


def database(
    work: Workspace, relative: str, transform: Callable[[sqlite3.Connection], bool]
) -> None:
    source = work.source(relative)
    if not source.exists():
        return
    directory = work.temporary()
    directory.mkdir()
    target = directory / "data.sqlite3"
    group = ("", "-wal", "-shm", "-journal")
    for suffix in group:
        original = work.source(relative + suffix)
        # SHM is a coordination cache. Rebuild it on the private copy.
        if original.exists() and suffix != "-shm":
            shutil.copyfile(original, target.with_name(target.name + suffix))
    try:
        with closing(sqlite3.connect(target)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA foreign_keys=OFF")
            # This disposable database cannot be published until Workspace syncs
            # its closed, validated main file together with the rollback material.
            connection.execute("PRAGMA synchronous=OFF")
            require_structure(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') LIMIT 1"
                ).fetchone()
                is None,
                "SQLite",
                "custom triggers and views are outside the supported schema",
            )
            changed = transform(connection)
            connection.commit()
            if changed:
                check_integrity(connection)
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
        error = (
            UpgradeBlockedError
            if code
            in {
                sqlite3.SQLITE_CORRUPT,
                sqlite3.SQLITE_NOTADB,
                sqlite3.SQLITE_ERROR,
            }
            else UpgradeError
        )
        raise error(f"{relative}: SQLite validation failed ({type(exc).__name__})") from exc
    except UpgradeDataError as exc:
        raise type(exc)(f"{relative}: {exc}") from exc
    if changed:
        work.stage(relative, target)
        for suffix in group[1:]:
            work.stage(relative + suffix, None)


def check_integrity(connection: sqlite3.Connection) -> None:
    require_structure(
        connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
        "SQLite",
        "integrity check failed",
    )
    require_structure(
        connection.execute("PRAGMA foreign_key_check").fetchone() is None,
        "SQLite",
        "foreign key check failed",
    )


def payloads(
    connection: sqlite3.Connection,
    table: str,
    kind: str,
    *,
    version: int = 0,
    owner: tuple[str, str] | None = None,
) -> bool:
    """Only internal table names enter SQL; preserve keys and sequence allocation."""
    changed = False
    query = f"SELECT rowid AS upgrade_rowid, * FROM {table}"
    cursor = connection.execute(
        query + (f" WHERE {owner[0]}=?" if owner else ""),
        (owner[1],) if owner else (),
    )
    while rows := cursor.fetchmany(128):
        for row in rows:
            raw = loads(row["payload"])
            value = convert_task(kind, raw, version) if version else convert(kind, raw)
            if value is not raw:
                require(
                    all(row[key] == value.get(key) for key in _IDENTITIES[kind]),
                    table,
                    "record projection identity mismatch",
                )
                connection.execute(
                    f"UPDATE {table} SET payload=? WHERE rowid=?",
                    (dumps(value), row["upgrade_rowid"]),
                )
                changed = True
    return changed


def usage(connection: sqlite3.Connection) -> bool:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    require_structure(version in (1, 2), "usage", "expected database schema 1 or 2")
    require_structure(
        connection.execute("PRAGMA application_id").fetchone()[0] == 0x4F555242,
        "usage",
        "unexpected database application id",
    )
    changed = payloads(connection, "entry_records", "usage")
    changed = payloads(connection, "events", "usage_event") or changed
    if version == 1:
        connection.execute(
            USAGE_SCHEMA[0].replace("CREATE TABLE entries", "CREATE TABLE new_entries")
        )
        columns = [row[1] for row in connection.execute("PRAGMA table_info(entries)")]
        expected = {row[1] for row in connection.execute("PRAGMA table_info(new_entries)")}
        require_structure(
            set(columns) == expected - {"plugin_id"}, "usage", "unknown entries columns"
        )
        names = ",".join(columns)
        connection.execute(f"INSERT INTO new_entries ({names}) SELECT {names} FROM entries")
        connection.execute("DROP TABLE entries")
        connection.execute("ALTER TABLE new_entries RENAME TO entries")
        for statement in USAGE_SCHEMA[4:]:
            connection.execute(statement)
        connection.execute("PRAGMA user_version=2")
        changed = True
    # Preserve the projection; audit history includes resets and is not replayed.
    return changed


def tasks(connection: sqlite3.Connection) -> bool:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    require_structure(version in (1, 3), "tasks", "expected database schema 1 or 3")
    changed = payloads(connection, "tasks", "task", version=version)
    changed = payloads(connection, "task_executions", "execution", version=version) or changed
    if version == 1:
        connection.execute("ALTER TABLE tasks ADD COLUMN owner_id TEXT")
        connection.execute("CREATE INDEX tasks_owner ON tasks(owner_id, updated_at DESC, task_id)")
        connection.execute("PRAGMA user_version=3")
        changed = True
    return changed


_SESSION_TABLES = (
    ("sessions", "session"),
    ("messages", "message"),
    ("events", "event"),
    ("runs", "run"),
    ("compactions", "compaction"),
    ("latest_contexts", "context"),
    ("resource_references", "resource"),
    ("harness_versions", "harness"),
    ("session_commits", "commit"),
    ("delegations", "delegation"),
)


def sessions(connection: sqlite3.Connection, units: UpgradeUnits) -> bool:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key='sqlite_schema_version'"
    ).fetchone()
    require_structure(row is not None and row[0] == "7", "sessions", "expected database schema 7")
    # This table has no owner index in the source schema. Index the private copy
    # for per-session conversion, then drop it before publication.
    compaction_index = f"upgrade_compactions_{uuid4().hex}"
    connection.execute(f"CREATE INDEX {compaction_index} ON compactions(session_id)")
    changed = False
    # Snapshot identities so deleting failed units cannot disturb the cursor.
    identities = [row[0] for row in connection.execute("SELECT session_id FROM sessions")]
    units.work.progress("sessions", 0, len(identities))
    for completed, identity in enumerate(identities, 1):
        unit = UpgradeUnit("session", identity)
        result = units.run(
            unit,
            partial(_session, connection, identity),
            remove=partial(_remove_session, connection, units.work, identity),
            connection=connection,
        )
        changed = bool(result) or unit in units.discarded or changed
        units.work.progress("sessions", completed, len(identities))
    connection.execute(f"DROP INDEX {compaction_index}")
    return changed


def _session(connection: sqlite3.Connection, identity: str) -> bool:
    changed = False
    catalog_changed = False
    for table, kind in _SESSION_TABLES:
        key = "root_session_id" if kind == "delegation" else "session_id"
        table_changed = payloads(connection, table, kind, owner=(key, identity))
        changed |= table_changed
        if table in {"sessions", "messages", "runs", "resource_references"}:
            catalog_changed |= table_changed
    if not catalog_changed:
        return changed
    row = connection.execute(
        "SELECT payload FROM sessions WHERE session_id=?",
        (identity,),
    ).fetchone()
    session = validate(SessionRecord, loads(row["payload"]))
    require(identity == session.session_id, "sessions", "identity mismatch")
    latest_run = _one(connection, "runs", "run_id", session.last_run_id, session.session_id)
    latest_message = _one(
        connection, "messages", "message_id", session.last_message_id, session.session_id
    )
    projection = SessionCatalogProjection(
        latest_run=validate(RunRecord, latest_run) if latest_run else None,
        latest_message=validate(MessageRecord, latest_message) if latest_message else None,
        message_count=_count(connection, "messages", session.session_id),
        run_count=_count(connection, "runs", session.session_id),
        resource_count=_count(connection, "resource_references", session.session_id),
    )
    entry = catalog_entry_from_projection(session, projection)
    connection.execute(
        "INSERT OR REPLACE INTO session_catalog(session_id,payload) VALUES (?,?)",
        (session.session_id, entry.model_dump_json()),
    )
    return changed


def _remove_session(connection: sqlite3.Connection, work: Workspace, identity: str) -> None:
    # Foreign keys are disabled on the private copy. Delete only this root's
    # owned rows; references to other sessions do not transfer their ownership.
    for table, kind in reversed(_SESSION_TABLES):
        key = "root_session_id" if kind == "delegation" else "session_id"
        connection.execute(f"DELETE FROM {table} WHERE {key}=?", (identity,))
    connection.execute("DELETE FROM session_catalog WHERE session_id=?", (identity,))
    relative = f"sessions/{identity}"
    # Resource storage can also be used by an unselected file backend.
    if not work.path(f"{relative}/session.json").exists():
        work.remove_directory(relative)


def remove_database(work: Workspace, relative: str) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        work.stage(relative + suffix, None)


def _one(
    connection: sqlite3.Connection, table: str, key: str, identity: str | None, session_id: str
) -> Any:
    if identity is None:
        return None
    row = connection.execute(
        f"SELECT payload FROM {table} WHERE {key}=? AND session_id=?", (identity, session_id)
    ).fetchone()
    require(row is not None, table, "missing Session head reference")
    assert row is not None
    return loads(row[0])


def _count(connection: sqlite3.Connection, table: str, session_id: str) -> int:
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE session_id=?", (session_id,)
        ).fetchone()[0]
    )
