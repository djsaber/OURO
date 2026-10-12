import multiprocessing
import time

import pytest

from ouro.local_locks import FileAccessLock


def hold_access(path, exclusive, ready):
    with FileAccessLock(path, exclusive=exclusive):
        ready.set()
        time.sleep(15)


@pytest.mark.parametrize("exclusive", [False, True])
def test_access_lock_coordinates_processes_and_releases_after_owner_exit(tmp_path, exclusive):
    path = tmp_path / "access.lock"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(target=hold_access, args=(path, exclusive, ready))
    process.start()
    reader, writer = FileAccessLock(path), FileAccessLock(path, exclusive=True)
    try:
        assert ready.wait(10)
        assert not writer.acquire()
        assert reader.acquire() is not exclusive
        reader.release()
        process.terminate()
        process.join(10)
        assert not process.is_alive()
        assert writer.acquire()
        assert not reader.acquire()
        writer.release()
        assert reader.acquire()
    finally:
        reader.release()
        writer.release()
        if process.is_alive():
            process.terminate()
        process.join(10)
        process.close()


def test_independent_readers_in_one_process_hold_until_all_are_released(tmp_path):
    path = tmp_path / "access.lock"
    writer = FileAccessLock(path, exclusive=True)
    with FileAccessLock(path):
        second = FileAccessLock(path)
        assert second.acquire()
        assert not writer.acquire()
    try:
        assert not writer.acquire()
        second.release()
        assert writer.acquire()
        with pytest.raises(RuntimeError, match="in use"):
            with FileAccessLock(path):
                pytest.fail("maintenance admitted a reader")
    finally:
        second.release()
        writer.release()
        writer.release()
    with FileAccessLock(path):
        pass
