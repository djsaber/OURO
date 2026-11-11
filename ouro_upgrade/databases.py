"""Check immutable headers and transform private SQLite copies."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from ouro.billing.local import (
    _APPLICATION_ID as USAGE_APPLICATION_ID,
)
from ouro.billing.local import (
    _SCHEMA_VERSION as USAGE_SCHEMA_VERSION,
)
from ouro.billing.local import (
    LocalUsageLedger,
)
from ouro.billing.schema import USAGE_SCHEMA
from ouro.core.billing import ProviderUsageEntry
from ouro_upgrade.errors import UpgradeDataError, UpgradeError, require
from ouro_upgrade.json_codec import dumps, loads
from ouro_upgrade.records import convert, convert_task, validate
from ouro_upgrade.steps import UpgradeStep, plan
from ouro_upgrade.workspace import Workspace

# The task repository has no exported container-version constant.
TASK_SCHEMA_VERSION = 3

_IDENTITIES = {
    "task": ("task_id", "revision"),
    "execution": ("task_id", "execution_id"),
    "usage": ("entry_id",),
    "usage_event": ("event_id",),
}


def database(
    work: Workspace,
    relative: str,
    transform: Callable[[sqlite3.Connection], bool | Path],
    *,
    current: Callable[[sqlite3.Connection], bool] | None = None,
) -> None:
    source = work.path(relative)
    if not source.exists():
        return
    # Immutable reads cannot create SHM or recover a journal. Use this fast path
    # only when the stopped database has no uncheckpointed sidecar state.
    if current is not None and not any(
        work.path(relative + suffix).exists() for suffix in ("-wal", "-journal")
    ):
        try:
            with closing(sqlite3.connect(source.as_uri() + "?immutable=1", uri=True)) as probe:
                if current(probe):
                    return
        except sqlite3.DatabaseError as exc:
            if getattr(exc, "sqlite_errorcode", 0) & 0xFF in {
                sqlite3.SQLITE_CORRUPT,
                sqlite3.SQLITE_NOTADB,
                sqlite3.SQLITE_ERROR,
            }:
                raise UpgradeDataError(f"{relative}: invalid SQLite structure") from exc
            raise
    work.source(relative)
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
            require(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') LIMIT 1"
                ).fetchone()
                is None,
                "SQLite",
                "custom triggers and views are outside the supported schema",
            )
            changed = transform(connection)
            connection.commit()
            if changed is True:
                check_integrity(connection)
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
        error = (
            UpgradeDataError
            if code
            in {
                sqlite3.SQLITE_CORRUPT,
                sqlite3.SQLITE_NOTADB,
                sqlite3.SQLITE_ERROR,
                sqlite3.SQLITE_CONSTRAINT,
            }
            else UpgradeError
        )
        raise error(f"{relative}: SQLite validation failed ({type(exc).__name__})") from exc
    except UpgradeDataError as exc:
        raise UpgradeDataError(f"{relative}: {exc}") from exc
    if isinstance(changed, Path):
        target = changed
        with closing(sqlite3.connect(target)) as connection:
            check_integrity(connection)
    if changed:
        work.stage(relative, target)
        for suffix in group[1:]:
            work.stage(relative + suffix, None)


def check_integrity(connection: sqlite3.Connection) -> None:
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


def payloads(
    connection: sqlite3.Connection,
    table: str,
    kind: str,
    *,
    version: int = 0,
) -> bool:
    """Only internal table names enter SQL; preserve keys and sequence allocation."""
    changed = False
    query = f"SELECT rowid AS upgrade_rowid, * FROM {table}"
    with closing(connection.execute(query)) as cursor:
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


def _usage_requests(connection: sqlite3.Connection) -> None:
    payloads(connection, "entry_records", "usage")
    payloads(connection, "events", "usage_event")
    # Preserve the effective ledger; derive raw request totals only from original reports.
    connection.execute(USAGE_SCHEMA[0].replace("CREATE TABLE entries", "CREATE TABLE new_entries"))
    columns = [row[1] for row in connection.execute("PRAGMA table_info(entries)")]
    names = ",".join(columns)
    connection.execute(f"INSERT INTO new_entries ({names}) SELECT {names} FROM entries")
    connection.execute("DROP TABLE entries")
    connection.execute("ALTER TABLE new_entries RENAME TO entries")
    connection.execute("ALTER TABLE entry_records DROP COLUMN source_event_id")
    for statement in USAGE_SCHEMA[4:]:
        connection.execute(statement)
    with closing(
        connection.execute("SELECT event_id, payload FROM events ORDER BY sequence")
    ) as rows:
        for row in rows:
            event = loads(row["payload"])
            if event["event_type"] != "record":
                continue
            entry = validate(ProviderUsageEntry, event["entry"])
            connection.execute(
                "INSERT INTO raw_requests VALUES (?, ?)", (entry.entry_id, row["event_id"])
            )
            if entry.run_id is not None:
                LocalUsageLedger._add_run_usage(connection, entry)


def _task_owners(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE tasks ADD COLUMN owner_id TEXT")
    connection.execute("CREATE INDEX tasks_owner ON tasks(owner_id, updated_at DESC, task_id)")


USAGE_STEPS = {1: UpgradeStep(5, _usage_requests), 2: UpgradeStep(5, _usage_requests)}
TASK_STEPS = {1: UpgradeStep(3, _task_owners)}


def usage(connection: sqlite3.Connection) -> bool:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    require(
        connection.execute("PRAGMA application_id").fetchone()[0] == USAGE_APPLICATION_ID,
        "usage",
        "unexpected database application id",
    )
    transitions = plan("usage SQLite", version, target=USAGE_SCHEMA_VERSION, steps=USAGE_STEPS)
    for step in transitions:
        step.transform(connection)
    if transitions:
        connection.execute(f"PRAGMA user_version={USAGE_SCHEMA_VERSION}")
    return bool(transitions)


def tasks(connection: sqlite3.Connection) -> bool:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    transitions = plan("tasks SQLite", version, target=TASK_SCHEMA_VERSION, steps=TASK_STEPS)
    if not transitions:
        return False
    for step in transitions:
        step.transform(connection)
    payloads(connection, "tasks", "task", version=version)
    payloads(connection, "task_executions", "execution", version=version)
    connection.execute(f"PRAGMA user_version={TASK_SCHEMA_VERSION}")
    return True


def remove_database(work: Workspace, relative: str) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        work.stage(relative + suffix, None)


def current_tasks(connection: sqlite3.Connection) -> bool:
    return connection.execute("PRAGMA user_version").fetchone()[0] == TASK_SCHEMA_VERSION


def current_usage(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute("PRAGMA user_version").fetchone()[0] == USAGE_SCHEMA_VERSION
        and connection.execute("PRAGMA application_id").fetchone()[0] == USAGE_APPLICATION_ID
    )
