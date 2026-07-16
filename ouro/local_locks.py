from __future__ import annotations

import errno
import importlib
import os
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Self
from weakref import WeakValueDictionary

_LOCAL_LOCKS_GUARD = threading.Lock()
_LOCAL_MUTATION_LOCKS: WeakValueDictionary[str, threading.RLock] = WeakValueDictionary()
_MUTATION_DEPTHS = threading.local()


class FileMutationLock:
    """Short, re-entrant cross-process lock for one local file mutation."""

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
            while not try_lock_descriptor(descriptor):
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
                        unlock_descriptor(self._descriptor)
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


def is_lock_held(path: Path) -> bool:
    """Return whether another process currently owns the lock at ``path``."""

    if not path.exists():
        return False
    try:
        descriptor = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        if not try_lock_descriptor(descriptor):
            return True
        unlock_descriptor(descriptor)
        return False
    finally:
        os.close(descriptor)


def try_lock_descriptor(descriptor: int) -> bool:
    """Attempt a non-blocking exclusive lock on an open file descriptor."""

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


def unlock_descriptor(descriptor: int) -> None:
    """Release a lock acquired by :func:`try_lock_descriptor`."""

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
