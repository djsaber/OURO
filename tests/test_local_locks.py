from __future__ import annotations

import multiprocessing
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ouro.local_locks import FileMutationLock, is_lock_held


def _hold_lock(path, ready, release) -> None:
    with FileMutationLock(path):
        ready.set()
        assert release.wait(15)


@pytest.fixture(params=["thread", "process"])
def contended_lock(tmp_path, request):
    path = tmp_path / "mutation.lock"
    context = multiprocessing.get_context("spawn")
    ready = context.Event() if request.param == "process" else threading.Event()
    release = context.Event() if request.param == "process" else threading.Event()
    worker = (
        context.Process(target=_hold_lock, args=(path, ready, release))
        if request.param == "process"
        else threading.Thread(target=_hold_lock, args=(path, ready, release))
    )
    worker.start()
    try:
        assert ready.wait(10)
        yield path, release
    finally:
        release.set()
        worker.join(10)
        if isinstance(worker, multiprocessing.process.BaseProcess):
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
            assert worker.exitcode == 0
        assert not worker.is_alive()


def test_timeout_releases_waiter_and_same_lock_can_be_reused(contended_lock) -> None:
    path, release = contended_lock
    lock = FileMutationLock(path)
    started = time.monotonic()
    assert not lock.acquire(timeout=0.03)
    assert time.monotonic() - started < 2
    assert is_lock_held(path)
    release.set()
    assert lock.acquire(timeout=5)
    lock.release()
    assert not is_lock_held(path)


def test_cancelled_waiter_cannot_acquire_after_owner_releases(contended_lock) -> None:
    path, release = contended_lock
    cancelled = threading.Event()
    entered = threading.Event()

    def wait() -> bool:
        lock = FileMutationLock(path)
        entered.set()
        try:
            return lock.acquire(timeout=10, cancel_event=cancelled)
        finally:
            lock.release()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(wait)
        assert entered.wait(5)
        cancelled.set()
        with pytest.raises(InterruptedError, match="cancelled"):
            future.result(timeout=2)
    release.set()
    fresh = FileMutationLock(path)
    assert fresh.acquire(timeout=5)
    fresh.release()


def test_zero_timeout_is_one_nonblocking_attempt(contended_lock) -> None:
    path, release = contended_lock
    lock = FileMutationLock(path)
    assert not lock.acquire(timeout=0)
    release.set()
    assert lock.acquire(timeout=5)
    lock.release()
    assert lock.acquire(timeout=0)
    lock.release()


def test_nested_locks_keep_outer_transaction_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "mutation.lock"
    outer = FileMutationLock(path)
    inner = FileMutationLock(path)
    assert outer.acquire(timeout=0)
    try:
        assert inner.acquire(timeout=0)
        inner.release()
        assert is_lock_held(path)
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert not executor.submit(FileMutationLock(path).acquire, timeout=0.03).result(2)
    finally:
        outer.release()
    assert not is_lock_held(path)


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("-inf"), float("nan")])
def test_invalid_timeout_does_not_create_lock_file(tmp_path: Path, timeout: float) -> None:
    path = tmp_path / "mutation.lock"
    with pytest.raises(ValueError, match="timeout"):
        FileMutationLock(path).acquire(timeout=timeout)
    assert not path.exists()


def test_precancelled_acquisition_does_not_create_file(tmp_path: Path) -> None:
    path = tmp_path / "mutation.lock"
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(InterruptedError):
        FileMutationLock(path).acquire(cancel_event=cancelled)
    assert not path.exists()


def test_io_failure_releases_local_lock(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "mutation.lock"
    lock = FileMutationLock(path)

    def fail(*args, **kwargs):
        raise OSError("fixture failure")

    with monkeypatch.context() as patch:
        patch.setattr("ouro.local_locks.os.open", fail)
        with pytest.raises(OSError, match="fixture"):
            lock.acquire(timeout=0)
    with ThreadPoolExecutor(max_workers=1) as executor:

        def acquire_and_release():
            peer = FileMutationLock(path)
            try:
                return peer.acquire(timeout=0.2)
            finally:
                peer.release()

        assert executor.submit(acquire_and_release).result(2)


def test_cancellation_racing_os_acquisition_releases_descriptor(tmp_path, monkeypatch) -> None:
    from ouro import local_locks

    path = tmp_path / "mutation.lock"
    cancelled = threading.Event()
    original = local_locks.try_lock_descriptor

    def acquire_then_cancel(descriptor):
        result = original(descriptor)
        cancelled.set()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(local_locks, "try_lock_descriptor", acquire_then_cancel)
        with pytest.raises(InterruptedError):
            FileMutationLock(path).acquire(timeout=0.2, cancel_event=cancelled)
    assert not is_lock_held(path)


@pytest.mark.skipif(os.name != "nt", reason="Windows extended paths")
def test_mutation_lock_reentry_accepts_extended_path_alias(tmp_path):
    path = tmp_path / "mutation.lock"
    extended = Path("\\\\?\\" + str(path))
    with FileMutationLock(extended):
        nested = FileMutationLock(path)
        assert nested.acquire(timeout=0)
        nested.release()
