"""Upgrade authoritative file records and rebuild their small catalog projections."""

from __future__ import annotations

from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

from ouro.automation.file_records import StoredTask, TaskFileIndex, TaskIndexEntry
from ouro.local_permissions import reject_link
from ouro_upgrade.errors import UpgradeDataError, UpgradeError, require
from ouro_upgrade.records import VERSION_STEPS, convert, loads, validate
from ouro_upgrade.units import UpgradeUnit, UpgradeUnits
from ouro_upgrade.workspace import Workspace


def decode(data: bytes) -> dict[str, Any]:
    try:
        return loads(data.decode("utf-8"))
    except UnicodeError:
        raise UpgradeDataError("invalid UTF-8 record") from None


def read_json(work: Workspace, relative: str) -> dict[str, Any]:
    try:
        return decode(work.read_bytes(relative))
    except UpgradeDataError as exc:
        raise UpgradeDataError(f"{relative}: {exc}") from exc
    except OSError as exc:
        raise UpgradeError(f"{relative}: cannot read record ({type(exc).__name__})") from exc


def record(work: Workspace, relative: str, kind: str) -> dict[str, Any]:
    return _record(work, relative, kind, read_json(work, relative))


def records(work: Workspace, paths: list[Path], kind: str) -> Iterator[tuple[Path, dict[str, Any]]]:
    for relative, data in work.read_many(path.relative_to(work.home).as_posix() for path in paths):
        try:
            raw = decode(data)
        except UpgradeDataError as exc:
            raise UpgradeDataError(f"{relative}: {exc}") from exc
        yield work.home / relative, _record(work, relative, kind, raw)


def _record(work: Workspace, relative: str, kind: str, raw: dict[str, Any]) -> dict[str, Any]:
    try:
        value = convert(kind, raw)
    except UpgradeDataError as exc:
        raise UpgradeDataError(f"{relative}: {exc}") from exc
    if value is not raw:
        work.json(relative, value)
    return value


def children(path: Path, pattern: str) -> list[Path]:
    reject_link(path)
    result = sorted(path.glob(pattern))
    for item in result:
        reject_link(item)
    return result


def tasks(work: Workspace, units: UpgradeUnits) -> int:
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
    for completed, directory in enumerate(directories, 1):
        relative = directory.relative_to(work.home).as_posix()
        result = units.run(
            UpgradeUnit("task", directory.name),
            partial(_task, work, directory),
            remove=partial(work.remove_directory, relative),
        )
        if result is not None:
            entries[directory.name] = result
        catalog_changed |= result is None or f"{relative}/task.json" in work.changes
        work.progress("tasks", completed, len(directories))
    if catalog_changed:
        work.json("tasks/index.json", TaskFileIndex(tasks=entries).model_dump(mode="json"))
        work.json(layout_path, {"schema_version": target})
    return len(entries)


def _task(work: Workspace, directory: Path) -> TaskIndexEntry:
    relative = directory.relative_to(work.home).as_posix()
    require((directory / "task.json").is_file(), relative, "missing task record")
    raw = record(work, f"{relative}/task.json", "task")
    task = validate(StoredTask, raw)
    require(task.task.task_id == directory.name, relative, "task identity mismatch")
    for _ in records(work, children(directory / "executions", "*.json"), "execution"):
        pass
    return TaskIndexEntry.from_record(task)
