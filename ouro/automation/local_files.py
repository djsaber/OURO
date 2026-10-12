from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from ouro.automation.file_records import (
    FileRecord,
    StoredExecution,
    StoredTask,
    TaskFileIndex,
    TaskFileTransaction,
    TaskIndexEntry,
)
from ouro.core.errors import PersistenceError, UnsupportedDataVersionError
from ouro.core.tasks import TaskConflictError, TaskCounts, TaskExecution, TaskRecord
from ouro.local_io import read_text_atomic, write_model_atomic, write_text_atomic
from ouro.local_locks import FileMutationLock, FileOwnerLock

_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class LocalTaskRepository:
    """Canonical JSON task files with recoverable commits and a small catalog.

    A durable intent makes task, execution and index publication one observable
    transaction. Every reader takes the mutation lock and finishes pending
    publication first. History is linked, so a page reads only its own receipts.
    """

    def __init__(self, home: Path) -> None:
        self.path = home / "tasks"
        self._records = self.path / "records"
        self._index_path = self.path / "index.json"
        self._intent_path = self.path / "transaction.json"
        self._mutation_path = home / "locks" / "task-data.lock"
        self._owner = FileOwnerLock(home / "locks" / "tasks.lock", owner={"component": "tasks"})
        self._owns_scheduler = False
        self._index: TaskFileIndex | None = None
        self._index_stamp: tuple[int, int, int] | None = None

    def initialize(self) -> None:
        with self._io_errors(), FileMutationLock(self._mutation_path):
            self.path.mkdir(parents=True, exist_ok=True)
            layout = self.path / "layout.json"
            if layout.exists():
                _read(FileRecord, layout)
            else:
                if self._records.exists() or self._intent_path.exists():
                    raise PersistenceError("Task file layout is missing; restore layout.json")
                write_model_atomic(layout, FileRecord())
            self._records.mkdir(exist_ok=True)
            self._recover()
            self._catalog()

    def acquire_scheduler(self) -> bool:
        if not self._owns_scheduler:
            self._owns_scheduler = self._owner.acquire()
        return self._owns_scheduler

    def release_scheduler(self) -> None:
        self._owner.release()
        self._owns_scheduler = False

    @contextmanager
    def _io_errors(self) -> Iterator[None]:
        try:
            yield
        except OSError as exc:
            raise PersistenceError(
                f"Task file storage failed ({type(exc).__name__}): {self.path}"
            ) from exc

    @contextmanager
    def _readable(self) -> Iterator[None]:
        with self._io_errors(), FileMutationLock(self._mutation_path):
            self._recover()
            yield

    def _directory(self, task_id: str) -> Path:
        _validate_id(task_id)
        directory = self._records / task_id
        if directory.is_symlink() or not directory.resolve().is_relative_to(
            self._records.resolve()
        ):
            raise PersistenceError("Task directory must remain within task storage")
        return directory

    def _task(self, task_id: str) -> StoredTask:
        path = self._directory(task_id) / "task.json"
        if not path.exists():
            raise KeyError(f"Task not found: {task_id}")
        record = _read(StoredTask, path)
        if record.task.task_id != task_id:
            raise PersistenceError("Task identity does not match its directory")
        return record

    def _execution(self, task_id: str, execution_id: str) -> StoredExecution:
        _validate_id(execution_id)
        record = _read(
            StoredExecution, self._directory(task_id) / "executions" / f"{execution_id}.json"
        )
        if record.execution.task_id != task_id or record.execution.execution_id != execution_id:
            raise PersistenceError("Execution identity does not match its task or file")
        return record

    def _catalog(self) -> TaskFileIndex:
        if not self._index_path.exists():
            self._rebuild_catalog()
        info = self._index_path.stat()
        stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
        if self._index is None or stamp != self._index_stamp:
            try:
                self._index = _read(TaskFileIndex, self._index_path)
                self._index_stamp = stamp
            except PersistenceError:
                # Only the catalog is disposable. Invalid canonical files still
                # fail with a diagnostic instead of silently omitting tasks.
                self._rebuild_catalog()
        assert self._index is not None
        return self._index

    def _rebuild_catalog(self) -> None:
        entries = {}
        for directory in self._records.iterdir():
            if directory.is_dir():
                try:
                    record = self._task(directory.name)
                except KeyError as exc:
                    raise PersistenceError(
                        f"Missing task.json while rebuilding task index: {directory}"
                    ) from exc
                entries[directory.name] = TaskIndexEntry.from_record(record)
        self._write_catalog(TaskFileIndex(tasks=entries))

    def _write_catalog(self, index: TaskFileIndex) -> None:
        write_model_atomic(self._index_path, index)
        self._index = index
        info = self._index_path.stat()
        self._index_stamp = (info.st_mtime_ns, info.st_size, info.st_ino)

    def get(self, task_id: str) -> TaskRecord:
        with self._readable():
            return self._task(task_id).task

    def list(
        self, *, limit: int = 200, offset: int = 0, owner_id: str | None = None
    ) -> tuple[TaskRecord, ...]:
        _validate_page(limit, offset)
        with self._readable():
            entries = self._catalog().tasks
            ids = sorted(
                task_id
                for task_id, entry in entries.items()
                if owner_id is None or entry.owner_id == owner_id
            )
            ids.sort(key=lambda task_id: entries[task_id].updated_at, reverse=True)
            return tuple(self._task(task_id).task for task_id in ids[offset : offset + limit])

    def counts(self) -> TaskCounts:
        with self._readable():
            entries = self._catalog().tasks.values()
            values: Counter[str] = Counter(entry.status for entry in entries)
            values["completed_executions"] = sum(entry.completed_executions for entry in entries)
            values["failed_executions"] = sum(entry.failed_executions for entry in entries)
            values["awaiting_approval"] = sum(
                entry.status == "paused"
                and entry.active_execution_id is not None
                and entry.awaiting_approval
                for entry in entries
            )
            return TaskCounts.model_validate(dict(values))

    def due(
        self, now: datetime, *, limit: int, after: tuple[datetime, str] | None = None
    ) -> tuple[TaskRecord, ...]:
        _validate_page(limit, 0)
        with self._readable():
            due = sorted(
                (entry.next_run_at, task_id)
                for task_id, entry in self._catalog().tasks.items()
                if entry.enabled
                and entry.active_execution_id is None
                and entry.next_run_at is not None
                and entry.next_run_at <= now
                and (after is None or (entry.next_run_at, task_id) > after)
            )
            return tuple(self._task(task_id).task for _, task_id in due[:limit])

    def next_due(self) -> datetime | None:
        with self._readable():
            return min(
                (
                    entry.next_run_at
                    for entry in self._catalog().tasks.values()
                    if entry.enabled
                    and entry.active_execution_id is None
                    and entry.next_run_at is not None
                ),
                default=None,
            )

    def active(self) -> tuple[TaskExecution, ...]:
        with self._readable():
            return tuple(
                self._execution(task_id, entry.active_execution_id).execution
                for task_id, entry in self._catalog().tasks.items()
                if entry.active_execution_id is not None
            )

    def executions(self, task_id: str, *, limit: int = 50) -> tuple[TaskExecution, ...]:
        _validate_page(limit, 0)
        with self._readable():
            if not (self._directory(task_id) / "task.json").exists():
                return ()
            current = self._task(task_id).history_head
            page: list[TaskExecution] = []
            seen: set[str] = set()
            while current is not None and len(page) < limit:
                if current in seen:
                    raise PersistenceError("Task execution history contains a cycle")
                seen.add(current)
                record = self._execution(task_id, current)
                page.append(record.execution)
                current = record.previous
            return tuple(page)

    def commit(
        self,
        task: TaskRecord,
        *,
        expected_revision: int | None,
        execution: TaskExecution | None = None,
    ) -> None:
        with self._readable():
            path = self._directory(task.task_id) / "task.json"
            previous = self._task(task.task_id) if path.exists() else None
            actual = previous.task.revision if previous else None
            if actual != expected_revision or task.revision != (actual or 0) + 1:
                raise TaskConflictError("Task changed; reload it before saving")
            if previous and previous.task.owner_id != task.owner_id:
                raise TaskConflictError("Task owner cannot change")
            stored = (previous or StoredTask(task=task)).model_copy(update={"task": task})
            receipt = None
            if execution is not None:
                if execution.task_id != task.task_id:
                    raise ValueError("Execution belongs to another task")
                if execution.owner_id != task.owner_id:
                    raise ValueError("Execution owner must match its task")
                _validate_id(execution.execution_id)
                marker = self._occurrence_path(execution)
                if marker.exists() and read_text_atomic(marker).strip() != execution.execution_id:
                    raise TaskConflictError("Scheduled occurrence was already claimed")
                execution_path = (
                    self._directory(task.task_id) / "executions" / f"{execution.execution_id}.json"
                )
                old = (
                    self._execution(task.task_id, execution.execution_id)
                    if execution_path.exists()
                    else None
                )
                if old and old.execution.scheduled_at != execution.scheduled_at:
                    raise TaskConflictError("Execution occurrence identity cannot change")
                if old and old.execution.target != execution.target:
                    raise TaskConflictError("Execution target cannot change after claim")
                if old and old.execution.cancel_requested:
                    execution = execution.model_copy(update={"cancel_requested": True})
                receipt = StoredExecution(
                    execution=execution, previous=old.previous if old else stored.history_head
                )
                stored = stored.model_copy(
                    update={
                        "history_head": stored.history_head if old else execution.execution_id,
                        "completed_executions": stored.completed_executions
                        + _outcome(execution, "completed")
                        - _outcome(old.execution if old else None, "completed"),
                        "failed_executions": stored.failed_executions
                        + _outcome(execution, "failed")
                        - _outcome(old.execution if old else None, "failed"),
                        "awaiting_approval": execution.awaiting_approval,
                    }
                )
            transaction = TaskFileTransaction(
                task_id=task.task_id, record=stored, execution=receipt
            )
            self._publish(transaction)

    def delete(self, task_id: str, *, expected_revision: int) -> None:
        with self._readable():
            try:
                task = self._task(task_id).task
            except KeyError as exc:
                raise TaskConflictError("Task changed or still has an active execution") from exc
            if task.revision != expected_revision or task.active_execution_id is not None:
                raise TaskConflictError("Task changed or still has an active execution")
            self._publish(TaskFileTransaction(task_id=task_id, record=None))

    def _occurrence_path(self, execution: TaskExecution) -> Path:
        timestamp = execution.scheduled_at.astimezone(UTC).isoformat(timespec="microseconds")
        key = hashlib.sha256(timestamp.encode("ascii")).hexdigest()
        return self._directory(execution.task_id) / "occurrences" / key

    def _publish(self, transaction: TaskFileTransaction) -> None:
        # Resolve or rebuild the catalog before accepting a durable intent.
        self._catalog()
        write_model_atomic(self._intent_path, transaction)
        self._apply(transaction)
        self._intent_path.unlink()

    def _recover(self) -> None:
        if self._intent_path.exists():
            self._apply(_read(TaskFileTransaction, self._intent_path))
            self._intent_path.unlink()

    def _apply(self, transaction: TaskFileTransaction) -> None:
        directory = self._directory(transaction.task_id)
        if transaction.record is None:
            if directory.exists():
                shutil.rmtree(directory)
        else:
            if transaction.record.task.task_id != transaction.task_id:
                raise PersistenceError("Task transaction identity mismatch")
            if transaction.execution is not None:
                execution = transaction.execution.execution
                if execution.task_id != transaction.task_id:
                    raise PersistenceError("Execution transaction identity mismatch")
                _validate_id(execution.execution_id)
                write_model_atomic(
                    directory / "executions" / f"{execution.execution_id}.json",
                    transaction.execution,
                )
                write_text_atomic(self._occurrence_path(execution), execution.execution_id)
            write_model_atomic(directory / "task.json", transaction.record)
        entries = dict(self._catalog().tasks)
        if transaction.record is None:
            entries.pop(transaction.task_id, None)
        else:
            entries[transaction.task_id] = TaskIndexEntry.from_record(transaction.record)
        self._write_catalog(TaskFileIndex(tasks=entries))


def _read[Record: FileRecord](model: type[Record], path: Path) -> Record:
    try:
        payload = json.loads(read_text_atomic(path))
        if (
            not isinstance(payload, dict)
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 3
        ):
            raise UnsupportedDataVersionError(
                f"Unsupported or missing task file schema_version: {path}; expected 3"
            )
        return model.model_validate(payload)
    except ValidationError as exc:
        issue = exc.errors(include_input=False, include_context=False)[0]
        field = ".".join(str(part) for part in issue["loc"]) or "record"
        raise PersistenceError(f"Invalid task file: {path}: {field} ({issue['type']})") from exc
    except ValueError as exc:
        raise PersistenceError(f"Invalid JSON in task file: {path}") from exc


def _outcome(execution: TaskExecution | None, status: str) -> int:
    return int(
        execution is not None and execution.ended_at is not None and execution.status == status
    )


def _validate_id(value: str) -> None:
    if not _RECORD_ID.fullmatch(value):
        raise ValueError("Invalid task storage record id")


def _validate_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= 1000 or offset < 0:
        raise ValueError("Page limit must be 1–1000 and offset must be nonnegative")
