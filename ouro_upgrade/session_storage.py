"""Upgrade native Session containers while preserving message bodies and execution facts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ouro.core.inputs import InputState
from ouro.sessions.sqlite_observation import OBSERVATION_SCHEMA
from ouro_upgrade.databases import database
from ouro_upgrade.errors import UpgradeDataError, require
from ouro_upgrade.files import children, record
from ouro_upgrade.records import MODELS, convert, dumps, loads, validate
from ouro_upgrade.retired_fields import sql_predicate
from ouro_upgrade.workspace import Workspace


def observation_schema(connection: sqlite3.Connection) -> None:
    """Establish a baseline position while leaving fact bodies and identities intact."""
    for table in ("runs", "tool_call_states", "compactions", "input_states"):
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN modified_revision INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute("DROP TABLE events")
    connection.executescript(OBSERVATION_SCHEMA)


def record_predicate(kind: str) -> str:
    """Select headers requiring conversion without loading message or request bodies."""
    version = "json_extract(payload, '$.schema_version') IS NOT ?"
    retired = sql_predicate(kind)
    if retired:
        version += " OR " + retired
    if kind == "run":
        catalog_version = MODELS["agent_catalog"].model_fields["schema_version"].default
        return (
            version
            + f""" OR (
            json_type(payload, '$.agent_catalog') = 'object' AND (
                json_extract(payload, '$.agent_catalog.schema_version') IS NOT {catalog_version}
                OR json_type(payload, '$.agent_catalog.spawn_policy.allowed_lifecycle_bindings')
                   IS NOT NULL
                OR EXISTS (SELECT 1 FROM json_each(payload, '$.agent_catalog.entries')
                           WHERE json_type(value, '$.definition.can_spawn') IS NOT NULL)
            ))"""
        )
    if kind == "delegation":
        return version + " OR json_type(payload, '$.definition.can_spawn') IS NOT NULL"
    return version


def current_records(connection: sqlite3.Connection, *, include_delegations: bool = False) -> bool:
    tables = [("sessions", "session"), ("runs", "run"), ("tool_call_states", "tool_state")]
    if include_delegations:
        tables.extend((("delegations", "delegation"), ("harness_versions", "harness")))
    if (
        include_delegations
        and connection.execute(
            "SELECT 1 FROM session_catalog "
            "WHERE json_type(payload, '$.title_locked') IS NOT NULL LIMIT 1"
        ).fetchone()
    ):
        return False
    return all(
        connection.execute(
            f"SELECT 1 FROM {table} WHERE {record_predicate(kind)} LIMIT 1",
            (MODELS[kind].model_fields["schema_version"].default,),
        ).fetchone()
        is None
        for table, kind in tables
    )


def current_file(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_revisions'"
    ).fetchone() is not None and current_records(connection)


def authority_records(connection: sqlite3.Connection, *, include_delegations: bool = False) -> None:
    tables = [("sessions", "session"), ("runs", "run"), ("tool_call_states", "tool_state")]
    if include_delegations:
        tables.extend((("delegations", "delegation"), ("harness_versions", "harness")))
    for table, kind in tables:
        rows = connection.execute(
            f"SELECT rowid, payload FROM {table} WHERE {record_predicate(kind)}",
            (MODELS[kind].model_fields["schema_version"].default,),
        )
        for identity, payload in rows:
            value = convert(kind, loads(payload))
            connection.execute(
                f"UPDATE {table} SET payload=? WHERE rowid=?", (dumps(value), identity)
            )

    if include_delegations:
        connection.execute(
            "UPDATE session_catalog SET payload=json_remove(payload, '$.title_locked') "
            "WHERE json_type(payload, '$.title_locked') IS NOT NULL"
        )


def file_session(work: Workspace, directory: Path) -> None:
    relative = directory.relative_to(work.home).as_posix()
    if (directory / "commit.json").exists():
        raise UpgradeDataError(
            f"{relative}: unfinished Session commit; recover using original OURO"
        )

    def transform(connection: sqlite3.Connection) -> bool:
        if current_file(connection):
            return False
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_revisions'"
            ).fetchone()
            is None
        ):
            connection.execute(
                "CREATE TABLE input_states (session_id TEXT NOT NULL, input_id TEXT NOT NULL, "
                "payload TEXT NOT NULL, PRIMARY KEY(session_id, input_id), "
                "FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE)"
            )
            observation_schema(connection)
            sources = (
                path.relative_to(work.home).as_posix()
                for path in children(directory / "inputs/states", "*.json")
            )
            for name, data in work.read_many(sources):
                try:
                    payload = data.decode("utf-8")
                except UnicodeError:
                    raise UpgradeDataError(f"{name}: invalid UTF-8 input state") from None
                state = validate(InputState, loads(payload))
                require(state.session_id == directory.name, name, "foreign input state")
                connection.execute(
                    "INSERT INTO input_states(session_id, input_id, payload) VALUES (?, ?, ?)",
                    (state.session_id, state.input_id, payload),
                )
                work.stage(name, None)
        authority_records(connection)
        return True

    database(
        work,
        f"{relative}/session-data.sqlite3",
        transform,
        current=current_file,
    )
    for path in children(directory / "delegations", "*.json"):
        record(work, path.relative_to(work.home).as_posix(), "delegation")

    for path in children(directory / "harness", "*.json"):
        record(work, path.relative_to(work.home).as_posix(), "harness")
