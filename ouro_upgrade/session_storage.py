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
from ouro_upgrade.workspace import Workspace


def observation_schema(connection: sqlite3.Connection) -> None:
    """Establish a baseline position while leaving fact bodies and identities intact."""
    for table in ("runs", "tool_call_states", "compactions", "input_states"):
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN modified_revision INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute("DROP TABLE events")
    connection.executescript(OBSERVATION_SCHEMA)


def current_file(connection: sqlite3.Connection) -> bool:
    # Successful units can already be current while the home still awaits others.
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_revisions'"
    ).fetchone() is not None and all(
        connection.execute(
            f"SELECT 1 FROM {table} "
            "WHERE json_extract(payload, '$.schema_version') IS NOT ? LIMIT 1",
            (MODELS[kind].model_fields["schema_version"].default,),
        ).fetchone()
        is None
        for table, kind in (("sessions", "session"), ("runs", "run"))
    )


def authority_records(connection: sqlite3.Connection, *, include_delegations: bool = False) -> None:
    tables = [("sessions", "session"), ("runs", "run")]
    if include_delegations:
        tables.append(("delegations", "delegation"))
    for table, kind in tables:
        rows = connection.execute(
            f"SELECT rowid, payload FROM {table} "
            "WHERE json_extract(payload, '$.schema_version') IS NOT ?",
            (MODELS[kind].model_fields["schema_version"].default,),
        )
        for identity, payload in rows:
            value = convert(kind, loads(payload))
            connection.execute(
                f"UPDATE {table} SET payload=? WHERE rowid=?", (dumps(value), identity)
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
