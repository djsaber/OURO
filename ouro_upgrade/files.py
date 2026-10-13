"""Upgrade authoritative file records and rebuild their small catalog projections."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC
from pathlib import Path
from typing import Any

from ouro.auth.file_permissions import reject_link
from ouro.automation.file_records import StoredTask, TaskFileIndex, TaskIndexEntry
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tasks import TaskExecution
from ouro.sessions.catalog import (
    SessionCatalogProjection,
    catalog_entry_from_projection,
    sort_catalog_entries,
)
from ouro.sessions.index import SessionIndex
from ouro_upgrade.errors import UpgradeError, require
from ouro_upgrade.records import convert, dumps, loads
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
    if value != raw:
        work.json(relative, value)
    return value


def journal(work: Workspace, relative: str, kind: str) -> Iterator[dict[str, Any]]:
    source = work.source(relative)
    if not source.exists():
        return
    temporary = work.temporary()
    with source.open("rb") as src, ExitStack() as opened:
        dst = None
        for number, line in enumerate(src, 1):
            try:
                raw = loads(line.decode("utf-8"))
                value = convert(kind, raw)
            except UpgradeError as exc:
                raise UpgradeError(f"{relative}:{number}: {exc}") from exc
            if raw != value and dst is None:
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
                dst.write(line if raw == value else dumps(value).encode("utf-8"))
            yield value
    if temporary.exists():
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
    require(layout.get("data_layout_version") == 9, "layout.json", "expected layout 9")
    initial_changes = len(work.changes)
    entries = []
    work.progress("sessions", 0, len(directories))
    for directory in directories:
        relative = directory.relative_to(work.home).as_posix()
        # Attachments for a SQLite-only store are not file Session records.
        require((directory / "session.json").is_file(), relative, "missing Session record")
        require(
            not work.source(f"{relative}/commit.json").exists(),
            relative,
            "unfinished Session commit; recover using its original OURO version",
        )
        raw_session = record(work, f"{relative}/session.json", "session")
        session = SessionRecord.model_validate(raw_session)
        require(session.session_id == directory.name, relative, "Session identity mismatch")
        harness = set()
        for path, value in records(work, children(directory / "harness", "*.json"), "harness"):
            require(
                value["session_id"] == session.session_id and value["version_id"] == path.stem,
                relative,
                "Harness identity mismatch",
            )
            harness.add(path.stem)
        require(session.harness_version in harness, relative, "missing current Harness checkpoint")
        runs = set()
        latest_run = None
        for path, value in records(work, children(directory / "runs", "*.json"), "run"):
            require(
                value["session_id"] == session.session_id and value["run_id"] == path.stem,
                relative,
                "Run identity mismatch",
            )
            runs.add(path.stem)
            if path.stem == session.last_run_id:
                latest_run = RunRecord.model_validate(value)
        for run_id in (session.last_run_id, session.active_run_id):
            require(run_id is None or run_id in runs, relative, "missing Session Run reference")
        resource_ids = _resources(work, relative)
        messages: set[str] = set()
        latest_message_raw = None
        for value in journal(work, f"{relative}/messages.jsonl", "message"):
            require(value["session_id"] == session.session_id, relative, "foreign message")
            require(value["message_id"] not in messages, relative, "duplicate message identity")
            require(
                value.get("sequence") == len(messages) + 1, relative, "invalid message sequence"
            )
            require(
                value.get("before_harness_version") in harness,
                relative,
                "missing message Harness checkpoint",
            )
            require(
                set(value.get("resource_refs", ())) <= resource_ids,
                relative,
                "missing message resource reference",
            )
            messages.add(value["message_id"])
            latest_message_raw = value
        latest_message = (
            MessageRecord.model_validate(latest_message_raw) if latest_message_raw else None
        )
        require(session.message_count == len(messages), relative, "Session message count mismatch")
        require(
            session.last_message_id == (latest_message.message_id if latest_message else None),
            relative,
            "Session message head mismatch",
        )
        previous_sequence = 0
        for value in journal(work, f"{relative}/events.jsonl", "event"):
            require(value["session_id"] == session.session_id, relative, "foreign event")
            require(value["sequence"] > previous_sequence, relative, "invalid event sequence")
            previous_sequence = value["sequence"]
        for value in journal(work, f"{relative}/compactions.jsonl", "compaction"):
            require(value["session_id"] == session.session_id, relative, "foreign compaction")
        for folder, kind in (("operations", "commit"), ("delegations", "delegation")):
            for _ in records(work, children(directory / folder, "*.json"), kind):
                pass
        if work.source(f"{relative}/context.json").exists():
            value = record(work, f"{relative}/context.json", "context")
            require(value["session_id"] == session.session_id, relative, "foreign context")
        projection = SessionCatalogProjection(
            latest_run=latest_run,
            latest_message=latest_message,
            message_count=len(messages),
            run_count=len(runs),
            resource_count=len(resource_ids),
        )
        entries.append(catalog_entry_from_projection(session, projection))
        work.progress("sessions", len(entries), len(directories))
    if len(work.changes) != initial_changes:
        work.json(
            "sessions/index.json",
            SessionIndex(sessions=sort_catalog_entries(entries)).model_dump(mode="json"),
        )
    return len(entries)


def _resources(work: Workspace, relative: str) -> set[str]:
    index = f"{relative}/resources/index.json"
    if not work.source(index).exists():
        return set()
    raw = read_json(work, index)
    require(set(raw) == {"resources"}, index, "unknown resource index structure")
    identities = set()
    for value in raw["resources"]:
        resource = convert("resource", value)
        resource_id = resource["resource_id"]
        require(resource_id not in identities, index, "duplicate resource identity")
        identities.add(resource_id)
        path = f"{relative}/resources/files/{resource['filename']}"
        source = work.source(path)
        require(
            source.exists() and source.stat().st_size == resource["size"],
            path,
            "missing or incomplete attachment",
        )
        require(
            f"sha256:{work.observed[path]}" == resource["digest"],
            path,
            "attachment digest mismatch",
        )
    return identities


def tasks(work: Workspace) -> int:
    layout_path = "tasks/layout.json"
    if not work.source(layout_path).exists():
        require(not (work.home / "tasks/records").exists(), "tasks", "missing layout")
        return 0
    require(
        not work.source("tasks/transaction.json").exists(),
        "tasks",
        "unfinished task transaction; recover using its original OURO version",
    )
    layout = read_json(work, layout_path)
    require(
        layout in ({"schema_version": 1}, {"schema_version": 3}),
        layout_path,
        "unsupported task file layout",
    )
    entries = {}
    initial_changes = len(work.changes)
    directories = children(work.home / "tasks/records", "*")
    work.progress("tasks", 0, len(directories))
    for directory in directories:
        relative = directory.relative_to(work.home).as_posix()
        raw = record(work, f"{relative}/task.json", "task")
        task = StoredTask.model_validate(raw)
        require(task.task.task_id == directory.name, relative, "task identity mismatch")
        executions: dict[str, str | None] = {}
        occurrences: dict[str, str] = {}
        for path, value in records(work, children(directory / "executions", "*.json"), "execution"):
            execution = value["execution"]
            require(
                execution["task_id"] == directory.name and execution["execution_id"] == path.stem,
                relative,
                "execution identity mismatch",
            )
            executions[path.stem] = value.get("previous")
            scheduled = TaskExecution.model_validate(execution).scheduled_at
            timestamp = scheduled.astimezone(UTC).isoformat(timespec="microseconds")
            occurrence = hashlib.sha256(timestamp.encode("ascii")).hexdigest()
            require(occurrence not in occurrences, relative, "duplicate scheduled occurrence")
            occurrences[occurrence] = path.stem
        visited = set()
        head = task.history_head
        while head is not None:
            require(
                head in executions and head not in visited,
                relative,
                "broken execution history chain",
            )
            visited.add(head)
            head = executions[head]
        require(visited == executions.keys(), relative, "unlinked execution history")
        for identity in (task.task.active_execution_id, task.task.last_execution_id):
            require(identity is None or identity in executions, relative, "missing task execution")
        for path in children(directory / "occurrences", "*"):
            relative_occurrence = path.relative_to(work.home).as_posix()
            identity = work.source(relative_occurrence).read_text("utf-8").strip()
            require(
                occurrences.pop(path.name, None) == identity,
                relative_occurrence,
                "occurrence identity mismatch",
            )
        require(not occurrences, relative, "missing occurrence marker")
        entries[directory.name] = TaskIndexEntry.from_record(task)
        work.progress("tasks", len(entries), len(directories))
    if layout["schema_version"] == 1 or len(work.changes) != initial_changes:
        work.json("tasks/index.json", TaskFileIndex(tasks=entries).model_dump(mode="json"))
        work.json(layout_path, {"schema_version": 3})
    return len(entries)
