from __future__ import annotations

import errno
import importlib
import json
import os
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Self
from weakref import WeakValueDictionary

from ouro.core.base import utc_now

_LOCAL_LOCKS_GUARD = threading.Lock()
_LOCAL_MUTATION_LOCKS: WeakValueDictionary[str, threading.RLock] = WeakValueDictionary()
_MUTATION_DEPTHS = threading.local()


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
        if not _try_lock_descriptor(descriptor):
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
            _unlock_descriptor(descriptor)
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        self._acquired = True
        return True

    def release(self) -> None:
        if not self._acquired or self._descriptor is None:
            return
        try:
            _unlock_descriptor(self._descriptor)
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


class FileMutationLock:
    """Short, re-entrant cross-process lock for one local persistence mutation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._key = str(path.resolve())
        with _LOCAL_LOCKS_GUARD:
            self._local_lock = _LOCAL_MUTATION_LOCKS.setdefault(self._key, threading.RLock())
        self._descriptor: int | None = None
        self._acquired = False
        self._outermost = False

    def acquire(self) -> bool:
        if self._acquired:
            return True
        self._local_lock.acquire()
        depths = _mutation_depths()
        depth = depths.get(self._key, 0)
        if depth:
            depths[self._key] = depth + 1
            self._acquired = True
            return True

        descriptor: int | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR)
            while not _try_lock_descriptor(descriptor):
                time.sleep(0.005)
            depths[self._key] = 1
            self._descriptor = descriptor
            self._outermost = True
            self._acquired = True
            return True
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            self._local_lock.release()
            raise

    def release(self) -> None:
        if not self._acquired:
            return
        depths = _mutation_depths()
        depth = depths.get(self._key, 0)
        try:
            if depth <= 1:
                depths.pop(self._key, None)
                if self._outermost and self._descriptor is not None:
                    try:
                        _unlock_descriptor(self._descriptor)
                    finally:
                        os.close(self._descriptor)
                        self._descriptor = None
            else:
                depths[self._key] = depth - 1
        finally:
            self._acquired = False
            self._local_lock.release()

    def __enter__(self) -> Self:
        self.acquire()
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


def is_lock_held(path: Path) -> bool:
    """Return whether another process currently owns the lock at ``path``."""

    if not path.exists():
        return False
    try:
        descriptor = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        if not _try_lock_descriptor(descriptor):
            return True
        _unlock_descriptor(descriptor)
        return False
    finally:
        os.close(descriptor)


def _try_lock_descriptor(descriptor: int) -> bool:
    try:
        _ensure_lock_byte(descriptor)
    except OSError as exc:
        # A peer may lock the first byte between our open and initialization on Windows.
        # Treat that sharing race exactly like a failed non-blocking lock acquisition.
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    try:
        if os.name == "nt":
            msvcrt = importlib.import_module("msvcrt")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        msvcrt = importlib.import_module("msvcrt")
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    fcntl = importlib.import_module("fcntl")
    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _ensure_lock_byte(descriptor: int) -> None:
    if os.fstat(descriptor).st_size > 0:
        return
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.write(descriptor, b"\0")
    os.fsync(descriptor)


def _mutation_depths() -> dict[str, int]:
    depths = getattr(_MUTATION_DEPTHS, "values", None)
    if depths is None:
        depths = {}
        _MUTATION_DEPTHS.values = depths
    return depths
