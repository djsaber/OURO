import sqlite3
import sys
from contextlib import closing

import pytest

from dev.benchmarks.sqlite_sync import sqlite_sync_calls

pytestmark = pytest.mark.skipif(
    sys.platform not in {"win32", "linux"}, reason="probe targets Windows and Linux VFS"
)


@pytest.fixture
def native_probe():
    try:
        with sqlite_sync_calls():
            pass
    except (RuntimeError, FileNotFoundError) as exc:
        pytest.skip(str(exc))


def test_probe_counts_real_durable_writes_and_leaves_reads_unsynced(tmp_path, native_probe):
    with closing(sqlite3.connect(tmp_path / "probe.db")) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("CREATE TABLE facts(value)")
        with sqlite_sync_calls() as writes:
            connection.execute("INSERT INTO facts VALUES (1)")
            connection.commit()
        assert sum(writes.values()) > 0
        with sqlite_sync_calls() as reads:
            assert connection.execute("SELECT value FROM facts").fetchone() == (1,)
        assert sum(reads.values()) == 0
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_probe_restores_original_calls_when_measurement_fails(tmp_path, native_probe):
    with closing(sqlite3.connect(tmp_path / "probe.db")) as connection:
        connection.execute("CREATE TABLE facts(value)")
        with pytest.raises(RuntimeError, match="interrupted"):
            with sqlite_sync_calls() as interrupted:
                raise RuntimeError("interrupted")
        connection.execute("INSERT INTO facts VALUES (1)")
        connection.commit()
        assert sum(interrupted.values()) == 0
        with sqlite_sync_calls() as next_measurement:
            connection.execute("INSERT INTO facts VALUES (2)")
            connection.commit()
        assert sum(next_measurement.values()) > 0
