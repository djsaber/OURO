"""Reuse native connections only while an execution owner retains the store."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from threading import Lock

from ouro.sessions.locks import SessionRunLock


class ExecutionConnections:
    """Borrow each connection exclusively; the last owner closes idle handles.

    Outside execution, query scopes close their handles normally. During a Run,
    native page/statement caches and the WAL survive individual transactions.
    Ordinary native handles let the last observer close the WAL after a writer
    exits; mode=ro handles can leave its files behind after the last read ends.
    No transaction survives a borrow and no caller waits for a pooled handle.
    """

    def __init__(self, database: Path, *, create: bool = True) -> None:
        self.database = database
        self._create = create
        self._lock = Lock()
        self._owners = 0
        self._idle: list[sqlite3.Connection] = []

    def retain(self) -> None:
        with self._lock:
            self._owners += 1

    def release(self) -> None:
        with self._lock:
            self._owners -= 1
            idle = self._idle if self._owners == 0 else []
            if not self._owners:
                self._idle = []
        for connection in idle:
            connection.close()

    def borrow(self) -> sqlite3.Connection:
        with self._lock:
            if self._idle:
                return self._idle.pop()
        target = self.database if self._create else self.database.absolute().as_uri() + "?mode=rw"
        connection = sqlite3.connect(
            target,
            uri=not self._create,
            timeout=5.0,
            check_same_thread=False,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def close_idle(self) -> None:
        """Release native file handles before replacing an owned Session directory."""
        with self._lock:
            idle, self._idle = self._idle, []
        for connection in idle:
            connection.close()

    def return_connection(self, connection: sqlite3.Connection) -> None:
        try:
            if connection.in_transaction:
                connection.rollback()
        except sqlite3.Error:
            connection.close()
            raise
        with self._lock:
            if self._owners:
                self._idle.append(connection)
                return
        connection.close()


class NativeSessionRunLock(SessionRunLock):
    """The existing execution lock also owns its backend's idle connections."""

    def __init__(
        self,
        path: Path,
        *,
        session_id: str,
        run_id: str,
        connections: ExecutionConnections,
        on_release: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(path, session_id=session_id, run_id=run_id)
        self._connections = connections
        self._retained = False
        self._on_release = on_release

    def acquire(self) -> bool:
        if not super().acquire():
            return False
        if not self._retained:
            self._connections.retain()
            self._retained = True
        return True

    def release(self) -> None:
        try:
            if self._retained:
                self._retained = False
                try:
                    self._connections.release()
                finally:
                    if self._on_release is not None:
                        self._on_release()
        finally:
            super().release()
