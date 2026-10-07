from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from ouro.automation.models import TaskConflictError, TaskCounts, TaskExecution, TaskRecord
from ouro.core.errors import PersistenceError
from ouro.local_locks import FileMutationLock, FileOwnerLock


class SQLiteTaskRepository:
    """Local task records, indexed independently of the conversation journals."""

    def __init__(self, home: Path) -> None:
        self.path = home / "tasks.sqlite3"
        self._owner = FileOwnerLock(home / "locks" / "tasks.lock", owner={"component": "tasks"})
        self._owns_scheduler = False

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5)
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        except sqlite3.Error as exc:
            raise PersistenceError(
                f"Task storage failed ({type(exc).__name__}): {self.path}"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    def initialize(self) -> None:
        lock = FileMutationLock(self.path.with_suffix(".init.lock"))
        if not lock.acquire(timeout=5):
            raise PersistenceError(f"task initialization lock timed out: {self.path}")
        try:
            self._initialize_schema()
        finally:
            lock.release()

    def _initialize_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise PersistenceError(f"Unsupported task store version: {version}; expected 1")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    enabled INTEGER NOT NULL, next_run_at TEXT,
                    active_execution_id TEXT, status TEXT NOT NULL,
                    updated_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_due ON tasks(next_run_at, task_id)
                    WHERE enabled = 1 AND active_execution_id IS NULL;
                CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
                CREATE TABLE IF NOT EXISTS task_executions (
                    execution_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    scheduled_at TEXT NOT NULL, created_at TEXT NOT NULL,
                    ended_at TEXT, payload TEXT NOT NULL,
                    UNIQUE(task_id, scheduled_at)
                );
                CREATE INDEX IF NOT EXISTS executions_history
                    ON task_executions(task_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS executions_outcome
                    ON task_executions(json_extract(payload, '$.status'))
                    WHERE ended_at IS NOT NULL;
                PRAGMA user_version=1;
            """)

    def acquire_scheduler(self) -> bool:
        if not self._owns_scheduler:
            self._owns_scheduler = self._owner.acquire()
        return self._owns_scheduler

    def release_scheduler(self) -> None:
        self._owner.release()
        self._owns_scheduler = False

    def get(self, task_id: str) -> TaskRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Task not found: {task_id}")
        return _decode(TaskRecord, row[0])

    def list(self, *, limit: int = 200, offset: int = 0) -> tuple[TaskRecord, ...]:
        _validate_page(limit, offset)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM tasks ORDER BY updated_at DESC, task_id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return tuple(_decode(TaskRecord, row[0]) for row in rows)

    def counts(self) -> TaskCounts:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM tasks GROUP BY status"
                " UNION ALL SELECT 'awaiting_approval', COUNT(*) FROM tasks t"
                " JOIN task_executions e ON t.active_execution_id=e.execution_id"
                " WHERE t.status='paused' AND json_extract(e.payload, '$.awaiting_approval')=1"
                " UNION ALL SELECT 'completed_executions', COUNT(*) FROM task_executions"
                " WHERE ended_at IS NOT NULL AND json_extract(payload, '$.status')='completed'"
                " UNION ALL SELECT 'failed_executions', COUNT(*) FROM task_executions"
                " WHERE ended_at IS NOT NULL AND json_extract(payload, '$.status')='failed'"
            ).fetchall()
        try:
            return TaskCounts.model_validate(dict(rows))
        except ValidationError as exc:
            raise PersistenceError("Task storage contains invalid status counts") from exc

    def due(
        self,
        now: datetime,
        *,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> tuple[TaskRecord, ...]:
        _validate_page(limit, 0)
        cursor_clause = " AND (next_run_at, task_id) > (?, ?)" if after else ""
        values: list[object] = [_timestamp(now)]
        if after:
            values.extend((_timestamp(after[0]), after[1]))
        values.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM tasks WHERE enabled=1 AND active_execution_id IS NULL "
                f"AND next_run_at<=?{cursor_clause} ORDER BY next_run_at, task_id LIMIT ?",
                values,
            ).fetchall()
        return tuple(_decode(TaskRecord, row[0]) for row in rows)

    def next_due(self) -> datetime | None:
        with self._connection() as connection:
            value = connection.execute(
                "SELECT MIN(next_run_at) FROM tasks WHERE enabled=1 AND active_execution_id IS NULL"
            ).fetchone()[0]
        return datetime.fromisoformat(value) if value is not None else None

    def active(self) -> tuple[TaskExecution, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT e.payload FROM tasks t JOIN task_executions e "
                "ON t.active_execution_id=e.execution_id"
            ).fetchall()
        return tuple(_decode(TaskExecution, row[0]) for row in rows)

    def executions(self, task_id: str, *, limit: int = 50) -> tuple[TaskExecution, ...]:
        _validate_page(limit, 0)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM task_executions WHERE task_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return tuple(_decode(TaskExecution, row[0]) for row in rows)

    def commit(
        self,
        task: TaskRecord,
        *,
        expected_revision: int | None,
        execution: TaskExecution | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM tasks WHERE task_id=?", (task.task_id,)
            ).fetchone()
            actual = row[0] if row else None
            if actual != expected_revision or task.revision != (actual or 0) + 1:
                raise TaskConflictError("Task changed; reload it before saving")
            connection.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET revision=excluded.revision, "
                "enabled=excluded.enabled, next_run_at=excluded.next_run_at, "
                "active_execution_id=excluded.active_execution_id, status=excluded.status, "
                "updated_at=excluded.updated_at, payload=excluded.payload",
                (
                    task.task_id,
                    task.revision,
                    task.enabled,
                    _timestamp(task.next_run_at),
                    task.active_execution_id,
                    task.status,
                    _timestamp(task.updated_at),
                    task.model_dump_json(),
                ),
            )
            if execution is not None:
                if execution.task_id != task.task_id:
                    raise ValueError("Execution belongs to another task")
                identity = (
                    execution.execution_id,
                    execution.task_id,
                    _timestamp(execution.scheduled_at),
                )
                occurrences = connection.execute(
                    "SELECT execution_id, task_id, scheduled_at FROM task_executions "
                    "WHERE execution_id=? OR (task_id=? AND scheduled_at=?)",
                    identity,
                ).fetchall()
                if any(occurrence != identity for occurrence in occurrences):
                    raise TaskConflictError(
                        "Execution identity conflicts with a persisted occurrence"
                    )
                connection.execute(
                    "INSERT INTO task_executions VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(execution_id) DO UPDATE SET ended_at=excluded.ended_at, "
                    "payload=excluded.payload",
                    (
                        execution.execution_id,
                        execution.task_id,
                        _timestamp(execution.scheduled_at),
                        _timestamp(execution.created_at),
                        _timestamp(execution.ended_at),
                        execution.model_dump_json(),
                    ),
                )

    def delete(self, task_id: str, *, expected_revision: int) -> None:
        with self._connection() as connection:
            result = connection.execute(
                "DELETE FROM tasks WHERE task_id=? AND revision=? AND active_execution_id IS NULL",
                (task_id, expected_revision),
            )
            if result.rowcount != 1:
                raise TaskConflictError("Task changed or still has an active execution")


def _decode[Record: BaseModel](model: type[Record], payload: str) -> Record:
    try:
        return model.model_validate_json(payload)
    except ValidationError as exc:
        issue = exc.errors(include_input=False, include_context=False)[0]
        field = ".".join(str(part) for part in issue["loc"]) or "record"
        raise PersistenceError(
            f"Invalid {model.__name__} in task storage: {field} ({issue['type']})"
        ) from exc


def _timestamp(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="microseconds") if value is not None else None


def _validate_page(limit: int, offset: int) -> None:
    if not 1 <= limit <= 1000 or offset < 0:
        raise ValueError("Page limit must be 1–1000 and offset must be nonnegative")
