"""Upgrade native Session containers while preserving message bodies and execution facts."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from ouro.core.inputs import InputState
from ouro.sessions.sqlite_observation import OBSERVATION_SCHEMA
from ouro_upgrade.databases import database
from ouro_upgrade.errors import UpgradeDataError, require
from ouro_upgrade.files import children, record
from ouro_upgrade.json_codec import dumps, loads
from ouro_upgrade.records import MODELS, NESTED_RECORDS, convert, validate
from ouro_upgrade.retired_fields import sql_predicate
from ouro_upgrade.steps import UpgradeStep, plan
from ouro_upgrade.workspace import Workspace


def observation_schema(connection: sqlite3.Connection) -> None:
    """Establish a baseline position while leaving fact bodies and identities intact."""
    for table in ("runs", "tool_call_states", "compactions", "input_states"):
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN modified_revision INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute("DROP TABLE events")
    connection.executescript(OBSERVATION_SCHEMA)


def _sqlite_observation(connection: sqlite3.Connection) -> None:
    observation_schema(connection)
    connection.execute("CREATE INDEX messages_sequence ON messages(session_id, sequence)")


def _sqlite_catalog_authority(connection: sqlite3.Connection) -> None:
    connection.execute(
        "UPDATE session_catalog SET payload=json_remove(payload, '$.workspace_trusted') "
        "WHERE json_type(payload, '$.workspace_trusted') IS NOT NULL"
    )


SQLITE_STEPS = {
    26: UpgradeStep(27, _sqlite_observation),
    27: UpgradeStep(28, _sqlite_catalog_authority),
}


def sqlite_schema(connection: sqlite3.Connection, version: int, target: int) -> bool:
    transitions = plan("Session SQLite", version, target=target, steps=SQLITE_STEPS)
    for step in transitions:
        step.transform(connection)
    if transitions:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='sqlite_schema_version'", (str(target),)
        )
    return bool(transitions)


def record_predicate(kind: str, *, column: str = "payload", prefix: str = "$") -> str:
    """Select the same owned versions and fields as the in-memory converter."""
    target = MODELS[kind].model_fields["schema_version"].default
    conditions = [
        f"json_type({column}, '{prefix}.schema_version') IS NOT 'integer'",
        f"json_extract({column}, '{prefix}.schema_version') IS NOT {target}",
    ]
    retired = sql_predicate(kind, column=column, prefix=prefix)
    if retired:
        conditions.append(retired)
    for path, child in NESTED_RECORDS.get(kind, ()):
        if ".*." in path:
            parent, field = path.split(".*.")
            alias = f"nested_{kind}"
            predicate = record_predicate(child, column=f"{alias}.value", prefix=f"$.{field}")
            conditions.append(
                f"EXISTS (SELECT 1 FROM json_each({column}, '{prefix}.{parent}') AS {alias} "
                f"WHERE {predicate})"
            )
        else:
            nested_path = f"{prefix}.{path}"
            predicate = record_predicate(child, column=column, prefix=nested_path)
            conditions.append(
                f"(json_type({column}, '{nested_path}') IS NOT NULL "
                f"AND json_type({column}, '{nested_path}') != 'null' AND ({predicate}))"
            )
    return " OR ".join(conditions)


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
        ).fetchone()
        is None
        for table, kind in tables
    )


def current_file(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_revisions'"
    ).fetchone() is not None and current_records(connection)


def authority_records(connection: sqlite3.Connection, *, include_delegations: bool = False) -> bool:
    changed = False
    tables = [("sessions", "session"), ("runs", "run"), ("tool_call_states", "tool_state")]
    if include_delegations:
        tables.extend((("delegations", "delegation"), ("harness_versions", "harness")))
    for table, kind in tables:
        with closing(
            connection.execute(
                f"SELECT rowid, payload FROM {table} WHERE {record_predicate(kind)}",
            )
        ) as rows:
            for identity, payload in rows:
                raw = loads(payload)
                value = convert(kind, raw)
                if value is raw:
                    continue
                connection.execute(
                    f"UPDATE {table} SET payload=? WHERE rowid=?", (dumps(value), identity)
                )
                changed = True

    if include_delegations:
        updated = connection.execute(
            "UPDATE session_catalog SET payload=json_remove(payload, '$.title_locked') "
            "WHERE json_type(payload, '$.title_locked') IS NOT NULL"
        ).rowcount
        changed = bool(updated) or changed
    return changed


def file_session(work: Workspace, directory: Path) -> None:
    relative = directory.relative_to(work.home).as_posix()
    if (directory / "commit.json").exists():
        raise UpgradeDataError(
            f"{relative}: unfinished Session commit; recover using original OURO"
        )

    def transform(connection: sqlite3.Connection) -> bool:
        changed = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_revisions'"
            ).fetchone()
            is None
        )
        if changed:
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
        return authority_records(connection) or changed

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
