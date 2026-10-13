"""Upgrade authoritative file records and rebuild their small catalog projections."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from ouro.auth.file_permissions import reject_link
from ouro.automation.file_records import StoredTask, TaskFileIndex, TaskIndexEntry
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions.catalog import (
    SessionCatalogProjection,
    catalog_entry_from_projection,
    sort_catalog_entries,
)
from ouro.sessions.index import SessionIndex
from ouro.sessions.layout import DATA_LAYOUT_VERSION
from ouro_upgrade.errors import UpgradeError, require
from ouro_upgrade.records import VERSION_STEPS, convert, dumps, loads, validate
from ouro_upgrade.workspace import Workspace


def read_json(work: Workspace, relative: str) -> dict[str, Any]:
    try:
        return loads(work.read_bytes(relative).decode("utf-8"))
    except UpgradeError as exc:
        raise UpgradeError(f"{relative}: {exc}") from exc
    except OSError as exc:
        raise UpgradeError(f"{relative}: cannot read record ({type(exc).__name__})") from exc


def record(work: Workspace, relative: str, kind: str) -> dict[str, Any]:
    return _record(work, relative, kind, read_json(work, relative))


def records(work: Workspace, paths: list[Path], kind: str) -> Iterator[tuple[Path, dict[str, Any]]]:
    for relative, data in work.read_many(path.relative_to(work.home).as_posix() for path in paths):
        try:
            raw = loads(data.decode("utf-8"))
        except UpgradeError as exc:
            raise UpgradeError(f"{relative}: {exc}") from exc
        yield work.home / relative, _record(work, relative, kind, raw)


def _record(work: Workspace, relative: str, kind: str, raw: dict[str, Any]) -> dict[str, Any]:
    try:
        value = convert(kind, raw)
    except UpgradeError as exc:
        raise UpgradeError(f"{relative}: {exc}") from exc
    if value is not raw:
        work.json(relative, value)
    return value


def journal(work: Workspace, relative: str, kind: str) -> Iterator[dict[str, Any]]:
    source = work.path(relative)
    if not source.exists():
        return
    temporary = work.temporary()
    checksum = hashlib.sha256()
    with source.open("rb") as src, ExitStack() as opened:
        dst = None
        for number, line in enumerate(src, 1):
            checksum.update(line)
            try:
                raw = loads(line.decode("utf-8"))
                value = convert(kind, raw)
            except UpgradeError as exc:
                raise UpgradeError(f"{relative}:{number}: {exc}") from exc
            if value is not raw and dst is None:
                dst = opened.enter_context(temporary.open("wb"))
                # Keep an unchanged prefix verbatim, without buffering the journal.
                remaining = src.tell() - len(line)
                with source.open("rb") as prefix:
                    while remaining:
                        block = prefix.read(min(remaining, 1024 * 1024))
                        require(bool(block), relative, "source changed while reading")
                        dst.write(block)
                        remaining -= len(block)
            if dst is not None:
                dst.write(line if value is raw else dumps(value).encode("utf-8"))
            yield value
    if temporary.exists():
        work.observed[relative] = checksum.hexdigest()
        work.stage(relative, temporary)


def children(path: Path, pattern: str) -> list[Path]:
    reject_link(path)
    result = sorted(path.glob(pattern))
    for item in result:
        reject_link(item)
    return result


def sessions(work: Workspace) -> int:
    root = work.home / "sessions"
    directories = [item for item in children(root, "*") if item.is_dir()]
    if not directories:
        return 0
    layout = read_json(work, "layout.json")
    require(
        layout.get("data_layout_version") == DATA_LAYOUT_VERSION,
        "layout.json",
        f"expected layout {DATA_LAYOUT_VERSION}",
    )
    catalog_changed = False
    entries = []
    work.progress("sessions", 0, len(directories))
    for directory in directories:
        relative = directory.relative_to(work.home).as_posix()
        # Attachments for a SQLite-only store are not file Session records.
        require((directory / "session.json").is_file(), relative, "missing Session record")
        require(
            not work.path(f"{relative}/commit.json").exists(),
            relative,
            "unfinished Session commit; recover using its original OURO version",
        )
        raw_session = record(work, f"{relative}/session.json", "session")
        session = validate(SessionRecord, raw_session)
        require(session.session_id == directory.name, relative, "Session identity mismatch")
        run_count = 0
        latest_run = None
        for path, value in records(work, children(directory / "runs", "*.json"), "run"):
            run_count += 1
            if path.stem == session.last_run_id:
                latest_run = validate(RunRecord, value)
        resource_count = _resources(work, relative)
        message_count = 0
        latest_message_raw = None
        for value in journal(work, f"{relative}/messages.jsonl", "message"):
            message_count += 1
            latest_message_raw = value
        latest_message = validate(MessageRecord, latest_message_raw) if latest_message_raw else None
        catalog_changed = catalog_changed or any(
            path in work.changes
            for path in (
                f"{relative}/session.json",
                f"{relative}/messages.jsonl",
                f"{relative}/resources/index.json",
                f"{relative}/runs/{session.last_run_id}.json",
            )
        )
        for filename, kind in (("events.jsonl", "event"), ("compactions.jsonl", "compaction")):
            for _ in journal(work, f"{relative}/{filename}", kind):
                pass
        if f"{relative}/events.jsonl" in work.changes:
            # Byte offsets belong to a particular journal representation.
            for path in children(directory / "event-index/runs", "*.json"):
                work.stage(path.relative_to(work.home).as_posix(), None)
            work.stage(f"{relative}/event-index/sequences.bin", None)
        for folder, kind in (
            ("harness", "harness"),
            ("operations", "commit"),
            ("delegations", "delegation"),
        ):
            for _ in records(work, children(directory / folder, "*.json"), kind):
                pass
        if work.path(f"{relative}/context.json").exists():
            record(work, f"{relative}/context.json", "context")
        projection = SessionCatalogProjection(
            latest_run=latest_run,
            latest_message=latest_message,
            message_count=message_count,
            run_count=run_count,
            resource_count=resource_count,
        )
        entries.append(catalog_entry_from_projection(session, projection))
        work.progress("sessions", len(entries), len(directories))
    if catalog_changed:
        work.json(
            "sessions/index.json",
            SessionIndex(sessions=sort_catalog_entries(entries)).model_dump(mode="json"),
        )
    return len(entries)


def _resources(work: Workspace, relative: str) -> int:
    index = f"{relative}/resources/index.json"
    if not work.path(index).exists():
        return 0
    raw = read_json(work, index)
    require(set(raw) == {"resources"}, index, "unknown resource index structure")
    converted = [convert("resource", value) for value in raw["resources"]]
    if any(
        value is not original for value, original in zip(converted, raw["resources"], strict=True)
    ):
        work.json(index, {"resources": converted})
    return len(converted)


def tasks(work: Workspace) -> int:
    layout_path = "tasks/layout.json"
    if not work.path(layout_path).exists():
        require(not (work.home / "tasks/records").exists(), "tasks", "missing layout")
        return 0
    require(
        not work.path("tasks/transaction.json").exists(),
        "tasks",
        "unfinished task transaction; recover using its original OURO version",
    )
    layout = read_json(work, layout_path)
    target = StoredTask.model_fields["schema_version"].default
    require(
        type(layout.get("schema_version")) is int
        and layout
        in ({"schema_version": version} for version in (*VERSION_STEPS.get("task", {}), target)),
        layout_path,
        "unsupported task file layout",
    )
    entries = {}
    catalog_changed = layout["schema_version"] != target
    directories = children(work.home / "tasks/records", "*")
    work.progress("tasks", 0, len(directories))
    for directory in directories:
        relative = directory.relative_to(work.home).as_posix()
        raw = record(work, f"{relative}/task.json", "task")
        task = validate(StoredTask, raw)
        require(task.task.task_id == directory.name, relative, "task identity mismatch")
        catalog_changed = catalog_changed or f"{relative}/task.json" in work.changes
        for _ in records(work, children(directory / "executions", "*.json"), "execution"):
            pass
        entries[directory.name] = TaskIndexEntry.from_record(task)
        work.progress("tasks", len(entries), len(directories))
    if catalog_changed:
        work.json("tasks/index.json", TaskFileIndex(tasks=entries).model_dump(mode="json"))
        work.json(layout_path, {"schema_version": target})
    return len(entries)
