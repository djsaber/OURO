"""Transform private SQLite copies; never open a user's source database."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from typing import Any

from ouro.billing.schema import USAGE_SCHEMA
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions.catalog import SessionCatalogProjection, catalog_entry_from_projection
from ouro_upgrade.errors import UpgradeError, require
from ouro_upgrade.records import convert, convert_task, dumps, loads
from ouro_upgrade.workspace import Workspace, copy_durable

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
            copy_durable(original, target.with_name(target.name + suffix))
    try:
        with closing(sqlite3.connect(target)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA foreign_keys=OFF")
            check_integrity(connection)
            changed = transform(connection)
            connection.commit()
            check_integrity(connection)
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.Error as exc:
        raise UpgradeError(f"{relative}: SQLite validation failed ({type(exc).__name__})") from exc
    except UpgradeError as exc:
        raise UpgradeError(f"{relative}: {exc}") from exc
    if changed:
        work.stage(relative, target)
        for suffix in group[1:]:
            work.stage(relative + suffix, None)


def check_integrity(connection: sqlite3.Connection) -> None:
    require(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') LIMIT 1"
        ).fetchone()
        is None,
        "SQLite",
        "custom triggers and views are outside the supported schema",
    )
    require(
        connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
        "SQLite",
        "integrity check failed",
    )
    require(
        connection.execute("PRAGMA foreign_key_check").fetchone() is None,
        "SQLite",
        "foreign key check failed",
    )


def payloads(connection: sqlite3.Connection, table: str, kind: str, *, version: int = 0) -> bool:
    """Only internal table names enter SQL; preserve keys and sequence allocation."""
    changed = False
    cursor = connection.execute(f"SELECT rowid AS upgrade_rowid, * FROM {table}")
    while rows := cursor.fetchmany(128):
        for row in rows:
            raw = loads(row["payload"])
            value = convert_task(kind, raw, version) if version else convert(kind, raw)
            require(
                all(row[key] == value.get(key) for key in _IDENTITIES[kind]),
                table,
                "record projection identity mismatch",
            )
            if raw != value:
                connection.execute(
                    f"UPDATE {table} SET payload=? WHERE rowid=?",
                    (dumps(value), row["upgrade_rowid"]),
                )
                changed = True
    return changed


def usage(connection: sqlite3.Connection) -> bool:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    require(version in (1, 2), "usage", "expected database schema 1 or 2")
    require(
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
        require(set(columns) == expected - {"plugin_id"}, "usage", "unknown entries columns")
        names = ",".join(columns)
        connection.execute(f"INSERT INTO new_entries ({names}) SELECT {names} FROM entries")
        connection.execute("DROP TABLE entries")
        connection.execute("ALTER TABLE new_entries RENAME TO entries")
        for statement in USAGE_SCHEMA[4:]:
            connection.execute(statement)
        connection.execute("PRAGMA user_version=2")
        changed = True
    # The projection is retained, not replayed from audit (which includes reset entries).
    require(
        connection.execute(
            "SELECT 1 FROM entries LEFT JOIN entry_records USING(entry_id) "
            "WHERE entry_records.entry_id IS NULL LIMIT 1"
        ).fetchone()
        is None,
        "usage",
        "missing current entry record",
    )
    for row in connection.execute("SELECT * FROM entries JOIN entry_records USING(entry_id)"):
        value = loads(row["payload"])
        for key in (
            "entry_id",
            "provider_id",
            "endpoint_id",
            "model_id",
            "session_id",
            "run_id",
            "workspace",
            "purpose",
            "plugin_id",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "estimated_cost",
        ):
            require(row[key] == value.get(key), "usage", "entry projection mismatch")
    return changed


def tasks(connection: sqlite3.Connection) -> bool:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    require(version in (1, 3), "tasks", "expected database schema 1 or 3")
    changed = payloads(connection, "tasks", "task", version=version)
    changed = payloads(connection, "task_executions", "execution", version=version) or changed
    if version == 1:
        connection.execute("ALTER TABLE tasks ADD COLUMN owner_id TEXT")
        connection.execute("CREATE INDEX tasks_owner ON tasks(owner_id, updated_at DESC, task_id)")
        connection.execute("PRAGMA user_version=3")
        changed = True
    for table, keys in (
        ("tasks", ("task_id", "revision", "status", "owner_id")),
        ("task_executions", ("execution_id", "task_id")),
    ):
        for row in connection.execute(f"SELECT * FROM {table}"):
            value = loads(row["payload"])
            require(all(row[key] == value[key] for key in keys), table, "record identity mismatch")
    return changed


def sessions(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key='sqlite_schema_version'"
    ).fetchone()
    require(row is not None and row[0] == "7", "sessions", "expected database schema 7")
    changed = False
    for table, kind in (
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
    ):
        changed = payloads(connection, table, kind) or changed
    for table, reference in (
        ("sessions", "harness_version"),
        ("messages", "before_harness_version"),
    ):
        missing = connection.execute(
            f"SELECT 1 FROM {table} AS record LEFT JOIN harness_versions AS harness "
            "ON record.session_id=harness.session_id "
            f"AND json_extract(record.payload, '$.{reference}')=harness.version_id "
            "WHERE harness.version_id IS NULL LIMIT 1"
        ).fetchone()
        require(missing is None, table, "missing Harness checkpoint")
    for row in connection.execute("SELECT session_id,payload FROM sessions"):
        session = SessionRecord.model_validate(loads(row["payload"]))
        require(row["session_id"] == session.session_id, "sessions", "identity mismatch")
        latest_run = _one(connection, "runs", "run_id", session.last_run_id, session.session_id)
        latest_message = _one(
            connection, "messages", "message_id", session.last_message_id, session.session_id
        )
        projection = SessionCatalogProjection(
            latest_run=RunRecord.model_validate(latest_run) if latest_run else None,
            latest_message=MessageRecord.model_validate(latest_message) if latest_message else None,
            message_count=_count(connection, "messages", session.session_id),
            run_count=_count(connection, "runs", session.session_id),
            resource_count=_count(connection, "resource_references", session.session_id),
        )
        require(projection.message_count == session.message_count, "sessions", "count mismatch")
        if changed:
            entry = catalog_entry_from_projection(session, projection)
            connection.execute(
                "INSERT OR REPLACE INTO session_catalog(session_id,payload) VALUES (?,?)",
                (session.session_id, entry.model_dump_json()),
            )
    return changed


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
