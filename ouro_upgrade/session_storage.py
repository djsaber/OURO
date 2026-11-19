"""Upgrade native Session containers while preserving message bodies and execution facts."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

from ouro.core.inputs import InputRecord, InputState
from ouro.core.message_roles import run_message_kind
from ouro.sessions.backends.files.database import HISTORY_INDEXES as FILE_HISTORY_INDEXES
from ouro.sessions.backends.sqlite.messages import HISTORY_INDEXES as SQLITE_HISTORY_INDEXES
from ouro.sessions.sqlite_inputs import INPUT_SCHEMA, write_input
from ouro.sessions.sqlite_observation import OBSERVATION_SCHEMA
from ouro_upgrade.databases import database
from ouro_upgrade.errors import UpgradeDataError, require
from ouro_upgrade.files import children, decode, record
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


def _sqlite_history(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX IF EXISTS messages_context_delivery")
    connection.execute("DROP INDEX IF EXISTS messages_input_history")
    connection.execute("DROP INDEX IF EXISTS messages_run_result_references")
    connection.execute("DROP INDEX IF EXISTS messages_assistant_turn")
    connection.execute("DROP INDEX IF EXISTS messages_assistant_run")
    connection.execute("ALTER TABLE messages RENAME COLUMN role TO kind")
    _convert_table(connection, "messages", "message")
    connection.execute(
        "UPDATE messages SET kind=CASE "
        "WHEN json_extract(payload, '$.input_ref') IS NOT NULL "
        "OR json_extract(payload, '$.metadata.context_provider_delivery_key') IS NOT NULL "
        "THEN 'input' "
        "WHEN json_extract(payload, '$.role')='assistant' THEN 'assistant_response' "
        "WHEN json_extract(payload, '$.role')='tool_result' THEN 'tool_result' ELSE 'input' END"
    )
    _convert_table(connection, "inputs", "input")
    _convert_table(connection, "input_states", "input_state")
    connection.executescript(SQLITE_HISTORY_INDEXES)


SQLITE_STEPS = {
    26: UpgradeStep(27, _sqlite_observation),
    27: UpgradeStep(28, _sqlite_catalog_authority),
    28: UpgradeStep(30, _sqlite_history),
    29: UpgradeStep(30, _sqlite_history),
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
    return (
        _has_schema_object(connection, "inputs")
        and _has_schema_object(connection, "session_revisions")
        and _has_schema_object(connection, "assistant_responses")
        and current_records(connection)
    )


def _has_schema_object(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
        is not None
    )


def _convert_table(connection: sqlite3.Connection, table: str, kind: str) -> bool:
    changed = False
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
    return changed


def authority_records(connection: sqlite3.Connection, *, include_delegations: bool = False) -> bool:
    changed = False
    tables = [("sessions", "session"), ("runs", "run"), ("tool_call_states", "tool_state")]
    if include_delegations:
        tables.extend((("delegations", "delegation"), ("harness_versions", "harness")))
    for table, kind in tables:
        changed = _convert_table(connection, table, kind) or changed

    if include_delegations:
        updated = connection.execute(
            "UPDATE session_catalog SET payload=json_remove(payload, '$.title_locked') "
            "WHERE json_type(payload, '$.title_locked') IS NOT NULL"
        ).rowcount
        changed = bool(updated) or changed
    return changed


def _file_history(work: Workspace, connection: sqlite3.Connection, journal: str) -> None:
    source = work.source(journal)
    target = work.temporary()
    connection.execute("DROP INDEX IF EXISTS context_deliveries")
    connection.execute("DROP INDEX IF EXISTS input_messages")
    connection.execute("DROP INDEX IF EXISTS assistant_runs")
    connection.execute("ALTER TABLE locations RENAME COLUMN role TO kind")
    with source.open("rb") as reader, target.open("wb") as writer:
        for line in reader:
            raw = decode(line)
            value = convert("message", raw)
            delivery_key = value.get("metadata", {}).get("context_provider_delivery_key")
            kind = run_message_kind(
                value["role"],
                supplied=value.get("input_ref") is not None or delivery_key is not None,
            )
            updated = connection.execute(
                "UPDATE locations SET offset=?, kind=?, assistant_turn_id=?, "
                "context_delivery_key=? WHERE message_id=?",
                (
                    writer.tell(),
                    kind,
                    value.get("turn_id") if kind == "assistant_response" else None,
                    delivery_key,
                    value["message_id"],
                ),
            )
            require(updated.rowcount == 1, journal, "message is missing its index entry")
            writer.write(line if value is raw else dumps(value).encode("utf-8"))
    work.stage(journal, target)
    connection.executescript(FILE_HISTORY_INDEXES)


def file_session(work: Workspace, directory: Path) -> None:
    relative = directory.relative_to(work.home).as_posix()
    if (directory / "commit.json").exists():
        raise UpgradeDataError(
            f"{relative}: unfinished Session commit; recover using original OURO"
        )

    def transform(connection: sqlite3.Connection) -> bool:
        connection.execute("PRAGMA journal_mode=WAL")
        changed = not _has_schema_object(connection, "session_revisions")
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
                state = validate(InputState, convert("input_state", loads(payload)))
                require(state.session_id == directory.name, name, "foreign input state")
                connection.execute(
                    "INSERT INTO input_states(session_id, input_id, payload) VALUES (?, ?, ?)",
                    (state.session_id, state.input_id, state.model_dump_json()),
                )
                work.stage(name, None)
        if not _has_schema_object(connection, "inputs"):
            connection.execute(INPUT_SCHEMA)
            expected = {
                row[0]
                for row in connection.execute(
                    "SELECT input_id FROM input_states WHERE session_id = ?", (directory.name,)
                )
            }
            sources = (
                path.relative_to(work.home).as_posix()
                for path in children(directory / "inputs/records", "*.json")
            )
            for name, data in work.read_many(sources):
                accepted = validate(InputRecord, convert("input", decode(data)))
                identity = accepted.item.queue_id
                require(
                    accepted.session_id == directory.name
                    and identity in expected
                    and Path(name).stem == hashlib.sha256(identity.encode()).hexdigest(),
                    name,
                    "accepted input does not match its stored identity or state",
                )
                write_input(connection, accepted)
                expected.remove(identity)
                work.stage(name, None)
            require(not expected, relative, "missing accepted input body")
            changed = True
        if not _has_schema_object(connection, "assistant_responses"):
            _file_history(work, connection, f"{relative}/messages.jsonl")
            _convert_table(connection, "inputs", "input")
            _convert_table(connection, "input_states", "input_state")
            changed = True
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
