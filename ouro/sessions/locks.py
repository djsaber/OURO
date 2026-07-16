from __future__ import annotations

import json
import os
from pathlib import Path
from types import TracebackType
from typing import Self

from ouro.core.base import utc_now
from ouro.local_locks import (
    FileMutationLock,
    try_lock_descriptor,
    unlock_descriptor,
)


class SessionRunLock:
    """Cross-process lock for one locally persisted session run."""

    def __init__(self, path: Path, *, session_id: str, run_id: str) -> None:
        self.path = path
        self.session_id = session_id
        self.run_id = run_id
        self._acquired = False
        self._descriptor: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR)
        if not try_lock_descriptor(descriptor):
            os.close(descriptor)
            return False
        try:
            payload = json.dumps(
                {
                    "session_id": self.session_id,
                    "run_id": self.run_id,
                    "pid": os.getpid(),
                    "created_at": utc_now(),
                },
                default=str,
            )
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, payload.encode("utf-8"))
            os.write(descriptor, b"\n")
            os.fsync(descriptor)
        except Exception:
            unlock_descriptor(descriptor)
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        self._acquired = True
        return True

    def release(self) -> None:
        if not self._acquired or self._descriptor is None:
            return
        try:
            unlock_descriptor(self._descriptor)
        finally:
            os.close(self._descriptor)
            self._descriptor = None
            self._acquired = False

    def __enter__(self) -> Self:
        if not self._acquired and not self.acquire():
            raise RuntimeError(f"session is already locked: {self.session_id}")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.release()


class SessionMutationLock(FileMutationLock):
    """File mutation lock carrying the owning Session identity for diagnostics."""

    def __init__(self, path: Path, *, session_id: str) -> None:
        super().__init__(path)
        self.session_id = session_id
