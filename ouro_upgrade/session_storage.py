"""Upgrade native Session containers without decoding retained business payloads."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ouro.core.inputs import InputState
from ouro.sessions.sqlite_observation import OBSERVATION_SCHEMA
from ouro_upgrade.databases import database
from ouro_upgrade.errors import UpgradeDataError, require
from ouro_upgrade.files import children
from ouro_upgrade.records import loads, validate
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
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_revisions'"
        ).fetchone()
        is not None
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
        return True

    database(
        work,
        f"{relative}/session-data.sqlite3",
        transform,
        current=current_file,
    )
