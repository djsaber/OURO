"""Publish native Session storage from supported historical containers."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from functools import partial
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ouro.core.harness import HarnessVersion
from ouro.core.resources import SessionResourceReference
from ouro.local_io import filesystem_path
from ouro.sessions import SQLiteSessionStore
from ouro.sessions.backends.files.database import initialize_index
from ouro.sessions.backends.files.message_locations import FileMessageIndex, FileMessageReference
from ouro.sessions.backends.sqlite.commits import SQLiteSessionCommits
from ouro.sessions.backends.sqlite.messages import write_messages
from ouro.sessions.catalog import SessionCatalogProjection, catalog_entry_from_projection
from ouro.sessions.index import SessionIndex, SessionIndexEntry, invalid_session_index_entry
from ouro.sessions.layout import DATA_LAYOUT_VERSION
from ouro.sessions.sqlite_compactions import insert_compaction
from ouro.sessions.sqlite_facts import write_call_state, write_facts
from ouro.sessions.sqlite_requests import write_header
from ouro.sessions.sqlite_resources import insert_reference
from ouro.sessions.sqlite_runs import write_run
from ouro.sessions.sqlite_sessions import write_session
from ouro_upgrade import session_records
from ouro_upgrade.delegations import resolve_file_parents, save_delegation
from ouro_upgrade.errors import UpgradeBlockedError, UpgradeDataError, require
from ouro_upgrade.files import children, decode, read_json
from ouro_upgrade.records import convert, loads, validate
from ouro_upgrade.units import UpgradeUnit, UpgradeUnits
from ouro_upgrade.workspace import Workspace


def write(
    connection: sqlite3.Connection,
    data: dict[str, list[dict[str, Any]]],
    *,
    directory: Path | None = None,
) -> SessionIndexEntry:
    try:
        return _write(connection, data, directory=directory)
    except (KeyError, TypeError, AttributeError, ValidationError) as exc:
        raise UpgradeDataError("incomplete or invalid historical Session facts") from exc


def _write(
    connection: sqlite3.Connection,
    data: dict[str, list[dict[str, Any]]],
    *,
    directory: Path | None = None,
) -> SessionIndexEntry:
    messages, runs, calls, states = session_records.messages_and_runs(
        data["message"], data["run"], data["session"][0].get("history_epoch", 0)
    )
    resources = [
        validate(SessionResourceReference, convert("resource", raw)) for raw in data["resource"]
    ]
    session = session_records.session(data["session"][0], runs, messages, resources)
    require(
        all(item.session_id == session.session_id for item in (*messages, *runs)),
        "session",
        "foreign message or Run",
    )
    compactions = session_records.compactions(data["compaction"], messages)
    require(
        all(item.session_id == session.session_id for item in compactions),
        "session",
        "foreign compaction",
    )
    header, body = session_records.context(next(iter(data["context"]), None))
    harness = [validate(HarnessVersion, convert("harness", raw)) for raw in data["harness"]]
    versions = {item.version_id: item for item in harness}
    missing_versions = {
        session.harness_version,
        *(item.before_harness_version for item in messages if item.before_harness_version),
    } - versions.keys()
    for identity in sorted(missing_versions):
        version = HarnessVersion(session_id=session.session_id, version_id=identity)
        harness.append(version)
        versions[identity] = version
    session.controls = versions[session.harness_version].state.controls
    write_session(connection, session)
    write_facts(connection, calls)
    for state in states:
        write_call_state(connection, state)
    if directory is None:
        write_messages(connection, messages)
        storage = SQLiteSessionCommits(connection)
        for version in harness:
            storage.save_version(version)
    else:
        references = []
        with (directory / "messages.jsonl").open("wb") as stream:
            for position, message in enumerate(messages, 1):
                references.append(
                    FileMessageReference.from_message(
                        message, position=position, offset=stream.tell()
                    )
                )
                stream.write((message.storage_json() + "\n").encode("utf-8"))
        FileMessageIndex().create(connection, references)
        (directory / "harness").mkdir()
        for version in harness:
            (directory / "harness" / f"{version.version_id}.json").write_text(
                version.model_dump_json(), "utf-8"
            )
    for run in runs:
        write_run(connection, run, history_epoch=session.history_epoch)
    for record in resources:
        insert_reference(connection, session.session_id, record)
    for record in compactions:
        insert_compaction(connection, record)
    if header is not None:
        write_header(connection, header)
        if directory is None:
            connection.execute(
                "INSERT INTO latest_contexts VALUES (?, ?, ?)",
                (session.session_id, header.context_id, body.model_dump_json()),
            )
        else:
            connection.execute(
                "INSERT INTO latest_contexts VALUES (?, ?)", (session.session_id, header.context_id)
            )
            (directory / "request-content").mkdir()
            (directory / "request-content" / f"{header.context_id}.json").write_text(
                body.model_dump_json(), "utf-8"
            )
    if directory is not None:
        for raw in data.get("delegation", []):
            save_delegation(connection, raw, directory=directory)
    return catalog_entry_from_projection(
        session,
        SessionCatalogProjection(
            latest_run=next((run for run in runs if run.run_id == session.last_run_id), None),
            latest_message=messages[-1] if messages else None,
            message_count=len(messages),
            run_count=len(runs),
            resource_count=len(resources),
        ),
    )


def file_session(work: Workspace, directory: Path) -> SessionIndexEntry:
    relative = directory.relative_to(work.home).as_posix()
    if not (directory / "session.json").is_file():
        raise UpgradeBlockedError(f"{relative}: missing Session record; ownership is unknown")
    if (directory / "commit.json").exists():
        raise UpgradeBlockedError(
            f"{relative}: unfinished Session commit; recover using original OURO"
        )
    data: dict[str, list[dict[str, Any]]] = {
        kind: []
        for kind in (
            "session",
            "run",
            "message",
            "compaction",
            "context",
            "resource",
            "harness",
            "delegation",
        )
    }
    old_paths = []
    for name, kind in (("session.json", "session"), ("context.json", "context")):
        path = f"{relative}/{name}"
        if work.path(path).exists():
            data[kind].append(read_json(work, path))
            old_paths.append(path)
    require(len(data["session"]) == 1, relative, "missing Session record")
    require(
        data["session"][0]["session_id"] == directory.name, relative, "Session identity mismatch"
    )
    for folder, kind in (("runs", "run"), ("harness", "harness"), ("delegations", "delegation")):
        for source_path in children(directory / folder, "*.json"):
            name = source_path.relative_to(work.home).as_posix()
            data[kind].append(read_json(work, name))
            old_paths.append(name)
    for name, kind in (
        ("messages.jsonl", "message"),
        ("compactions.jsonl", "compaction"),
    ):
        path = f"{relative}/{name}"
        if work.path(path).exists():
            data[kind] = [decode(line) for line in work.read_bytes(path).splitlines() if line]
            old_paths.append(path)
    path = f"{relative}/resources/index.json"
    if work.path(path).exists():
        resource_index = read_json(work, path)
        require(set(resource_index) == {"resources"}, path, "unknown resource index structure")
        data["resource"] = resource_index["resources"]
        old_paths.append(path)
    resolve_file_parents(work, directory.name, data["delegation"])
    temporary = filesystem_path(work.temporary())
    temporary.mkdir()
    target = temporary / "session-data.sqlite3"
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("PRAGMA synchronous=OFF")
        initialize_index(connection)
        entry = write(connection, data, directory=temporary)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")
    new_paths = {
        f"{relative}/{path.relative_to(temporary).as_posix()}"
        for path in temporary.rglob("*")
        if path.is_file()
    }
    for path in old_paths:
        if path not in new_paths:
            work.stage(path, None)
    for output_path in temporary.rglob("*"):
        if output_path.is_file():
            work.stage(f"{relative}/{output_path.relative_to(temporary).as_posix()}", output_path)
    # Obsolete notifications and receipts remain in the maintenance backup only.
    old_events = f"{relative}/events.jsonl"
    if work.path(old_events).exists():
        work.stage(old_events, None)
    for folder in ("operations", "event-index"):
        for obsolete_path in children(directory / folder, "**/*"):
            if obsolete_path.is_file():
                work.stage(obsolete_path.relative_to(work.home).as_posix(), None)
    return entry


def files(work: Workspace, units: UpgradeUnits) -> None:
    root = work.home / "sessions"
    if not work.path("layout.json").exists():
        require(not children(root, "*"), "sessions", "missing layout")
        return
    layout = read_json(work, "layout.json")
    version = layout.get("data_layout_version")
    require(
        type(version) is int and version in (9, DATA_LAYOUT_VERSION),
        "layout",
        "unsupported Session layout",
    )
    entries = []
    directories = [item for item in children(root, "*") if item.is_dir()]
    historical = [directory for directory in directories if (directory / "session.json").exists()]
    if version == DATA_LAYOUT_VERSION and not historical:
        return

    previous = (
        read_json(work, "sessions/index.json")
        if version == DATA_LAYOUT_VERSION and work.path("sessions/index.json").exists()
        else {"sessions": []}
    )
    current_entries = (
        {item.session_id: item for item in SessionIndex.model_validate(previous).sessions}
        if version == DATA_LAYOUT_VERSION
        else {}
    )
    work.progress("sessions", 0, len(directories))
    for number, directory in enumerate(directories, 1):
        if (directory / "session-data.sqlite3").exists():
            if directory.name in current_entries:
                entries.append(current_entries[directory.name])
            continue
        entry = units.run(
            UpgradeUnit("session", directory.name),
            partial(file_session, work, directory),
            remove=partial(work.remove_directory, directory.relative_to(work.home).as_posix()),
        )
        if entry is not None:
            entries.append(entry)
        elif UpgradeUnit("session", directory.name) not in units.discarded:
            entries.append(
                invalid_session_index_entry(
                    {},
                    error="Session upgrade failed",
                    fallback_session_id=directory.name,
                    health="unsupported_version",
                )
            )
        work.progress("sessions", number, len(directories))
    work.json("sessions/index.json", SessionIndex(sessions=entries).model_dump(mode="json"))
    work.json("layout.json", {**layout, "data_layout_version": DATA_LAYOUT_VERSION})


def sqlite(connection: sqlite3.Connection, units: UpgradeUnits) -> bool | Path:
    version = connection.execute(
        "SELECT value FROM metadata WHERE key='sqlite_schema_version'"
    ).fetchone()
    require(
        version is not None and version[0] in ("7", "27"), "sessions", "unsupported SQLite schema"
    )
    if version[0] == "27":
        return False
    work = units.work
    target = work.temporary()
    store = SQLiteSessionStore(target)
    # This unpublished database is exclusively owned by the upgrade workspace.
    store._initialize_schema()
    store._connections.close_idle()
    tables = {
        "session": "sessions",
        "run": "runs",
        "message": "messages",
        "context": "latest_contexts",
        "compaction": "compactions",
        "harness": "harness_versions",
        "resource": "resource_references",
    }
    connection.execute(
        "CREATE INDEX IF NOT EXISTS upgrade_compactions_owner ON compactions(session_id)"
    )
    with closing(sqlite3.connect(target)) as output:
        output.execute("PRAGMA synchronous=OFF")
        identities = connection.execute("SELECT session_id FROM sessions").fetchall()
        for number, (identity,) in enumerate(identities, 1):
            data = {
                kind: [
                    loads(row[0])
                    for row in connection.execute(
                        f"SELECT payload FROM {table} WHERE session_id=?"
                        + (" ORDER BY sequence" if kind == "message" else ""),
                        (identity,),
                    )
                ]
                for kind, table in tables.items()
            }
            entry = write(output, data)
            output.execute(
                "INSERT INTO session_catalog VALUES (?, ?)", (identity, entry.model_dump_json())
            )
            work.progress("sessions", number, len(identities))
        for row in connection.execute("SELECT payload FROM delegations"):
            save_delegation(output, loads(row[0]))
        output.commit()
        output.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        output.execute("PRAGMA journal_mode=DELETE")
    return target


def current_sqlite(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key='sqlite_schema_version'"
    ).fetchone()
    return row is not None and row[0] == "27"
