from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro_upgrade import workspace
from ouro_upgrade.engine import upgrade
from ouro_upgrade.errors import UpgradeError
from ouro_upgrade.records import loads
from ouro_upgrade.units import UpgradeUnit
from tests.upgrade.test_upgrade import hashes


def clone_files(home: Path, relative: str, identity: str) -> Path:
    source = home / relative
    target = source.with_name(identity)
    shutil.copytree(source, target)
    for path in target.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".jsonl"}:
            path.write_text(
                path.read_text("utf-8").replace(source.name, identity), encoding="utf-8"
            )
    return target


def clone_sqlite_session(home: Path) -> None:
    with closing(sqlite3.connect(home / "sessions.sqlite3")) as connection:
        connection.row_factory = sqlite3.Row
        for table in (
            "sessions",
            "session_catalog",
            "messages",
            "events",
            "runs",
            "compactions",
            "latest_contexts",
            "resource_references",
            "harness_versions",
            "session_commits",
        ):
            for row in connection.execute(
                f"SELECT * FROM {table} WHERE session_id='ses_fixture'"
            ).fetchall():
                values = {
                    key: value.replace("_fixture", "_bad").replace("msg_", "bad_msg_")
                    if isinstance(value, str)
                    else value
                    for key, value in dict(row).items()
                    if key != "row_id"
                }
                connection.execute(
                    f"INSERT INTO {table} ({','.join(values)}) VALUES "
                    f"({','.join('?' for _ in values)})",
                    tuple(values.values()),
                )
        connection.commit()
    resources = home / "sessions/ses_bad/resources/files"
    resources.mkdir(parents=True)
    (resources / "attachment.txt").write_bytes(b"failed session attachment")


def confirmed(report: dict) -> frozenset[UpgradeUnit]:
    return frozenset(
        UpgradeUnit(**failure["unit"]) for failure in report["failures"] if failure["discardable"]
    )


def test_file_failures_keep_successful_units_and_roll_back_failed_staging(historical_home):
    home = historical_home
    bad = clone_files(home, "sessions/ses_fixture", "ses_bad")
    (bad / "context.json").write_bytes(b'{"schema_version":999}')
    task = clone_files(home, "tasks/records/task_fixture", "task_bad")
    next((task / "executions").glob("*.json")).write_bytes(b"invalid JSON")
    (home / "config").mkdir()
    (home / "config/providers.json").write_bytes(b"invalid configuration")
    before = hashes(home)

    report = upgrade(home, isolate_failures=True)

    assert report["status"] == "partial"
    assert confirmed(report) == {
        UpgradeUnit("session", "ses_bad"),
        UpgradeUnit("task", "task_bad"),
        UpgradeUnit("configuration", "config/providers.json"),
    }
    after = hashes(home)
    for name, checksum in before.items():
        if name.startswith(("sessions/ses_bad/", "tasks/records/task_bad/", "config/")):
            assert after[name] == checksum
    assert (
        loads((home / "sessions/ses_fixture/context.json").read_text("utf-8"))["schema_version"]
        == 4
    )
    assert (
        loads((home / "tasks/records/task_fixture/task.json").read_text("utf-8"))["schema_version"]
        == 3
    )
    assert not (home / ".upgrade/in-progress.json").exists()

    result = upgrade(home, isolate_failures=True, discard=confirmed(report))
    assert result["status"] == "upgraded"
    assert not bad.exists() and not task.exists()
    assert not (home / "config/providers.json").exists()
    assert (home / "sessions/ses_fixture/resources/index.json").exists()
    assert upgrade(home)["status"] == "current"
    store = LocalSessionStore(home)
    store.initialize()
    assert len(store.load_session("ses_fixture").messages) == 3
    assert {
        entry["session_id"]
        for entry in loads((home / "sessions/index.json").read_text("utf-8"))["sessions"]
    } == {"ses_fixture"}
    assert set(loads((home / "tasks/index.json").read_text("utf-8"))["tasks"]) == {"task_fixture"}


@pytest.mark.parametrize("historical_home", ["sqlite"], indirect=True)
def test_sqlite_session_failure_isolated_from_other_rows_and_cleanup_owns_only_failed_session(
    historical_home,
):
    home = historical_home
    clone_sqlite_session(home)
    with closing(sqlite3.connect(home / "sessions.sqlite3")) as connection:
        connection.execute(
            "UPDATE latest_contexts SET payload=? WHERE session_id='ses_bad'",
            ('{"schema_version":999}',),
        )
        connection.commit()
        original_run = connection.execute(
            "SELECT payload FROM runs WHERE session_id='ses_bad'"
        ).fetchone()[0]
    options = dict(session_backend="sqlite", task_backend="sqlite", isolate_failures=True)

    report = upgrade(home, **options)

    assert report["status"] == "partial"
    assert confirmed(report) == {UpgradeUnit("session", "ses_bad")}
    with closing(sqlite3.connect(home / "sessions.sqlite3")) as connection:
        assert (
            connection.execute("SELECT payload FROM runs WHERE session_id='ses_bad'").fetchone()[0]
            == original_run
        )
        good_run = connection.execute(
            "SELECT payload FROM runs WHERE session_id='ses_fixture'"
        ).fetchone()[0]
        assert loads(good_run)["schema_version"] == 5
    after = hashes(home)
    result = upgrade(home, **options, discard=confirmed(report))
    assert result["status"] == "upgraded"
    with closing(sqlite3.connect(home / "sessions.sqlite3")) as connection:
        assert connection.execute("SELECT session_id FROM sessions").fetchall() == [
            ("ses_fixture",)
        ]
        assert (
            connection.execute(
                "SELECT payload FROM runs WHERE session_id='ses_fixture'"
            ).fetchone()[0]
            == good_run
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert not (home / "sessions/ses_bad").exists()
    for name, checksum in after.items():
        if name != "sessions.sqlite3" and not name.startswith("sessions/ses_bad/"):
            assert hashes(home)[name] == checksum
    assert upgrade(home, **options)["status"] == "current"
    store = SQLiteSessionStore(home / "sessions.sqlite3")
    store.initialize()
    assert len(store.load_session("ses_fixture").messages) == 3


def test_confirmation_rechecks_repaired_units_and_does_not_delete_new_failures(historical_home):
    home = historical_home
    path = home / "sessions/ses_fixture/context.json"
    original = path.read_bytes()
    path.write_bytes(b'{"schema_version":999}')
    first = upgrade(home, isolate_failures=True)
    path.write_bytes(original)
    task = home / "tasks/records/task_fixture/task.json"
    task.write_bytes(b'{"schema_version":999}')

    second = upgrade(home, isolate_failures=True, discard=confirmed(first))

    assert second["status"] == "partial"
    assert second["discarded"] == []
    assert confirmed(second) == {UpgradeUnit("task", "task_fixture")}
    assert loads(path.read_text("utf-8"))["schema_version"] == 4
    assert task.exists()


def test_shared_layout_failure_does_not_block_other_domains_or_offer_whole_home_deletion(
    historical_home,
):
    home = historical_home
    layout = home / "tasks/layout.json"
    layout.write_bytes(b'{"schema_version":999}')
    before = hashes(home)
    report = upgrade(home, isolate_failures=True)
    assert report["status"] == "partial"
    assert confirmed(report) == set()
    assert report["failures"][0]["unit"] == {"kind": "task_store", "identity": "files"}
    assert any(name.startswith("sessions/") for name in report["files"])
    assert all(
        hashes(home)[name] == value for name, value in before.items() if name.startswith("tasks/")
    )


def test_io_failure_is_not_a_discardable_record_error(historical_home, monkeypatch):
    original = workspace.Workspace.stage

    def fail(work, relative, source):
        if relative.startswith("sessions/"):
            raise OSError("disk full")
        original(work, relative, source)

    monkeypatch.setattr(workspace.Workspace, "stage", fail)
    before = hashes(historical_home)
    with pytest.raises(UpgradeError, match="I/O"):
        upgrade(
            historical_home,
            isolate_failures=True,
            discard=frozenset({UpgradeUnit("session", "ses_fixture")}),
        )
    assert hashes(historical_home) == before


@pytest.mark.parametrize("after_move", [False, True])
def test_interrupted_failed_unit_removal_restores_only_current_operation(
    historical_home, monkeypatch, after_move
):
    home = historical_home
    bad = clone_files(home, "sessions/ses_fixture", "ses_bad")
    (bad / "context.json").write_bytes(b'{"schema_version":999}')
    report = upgrade(home, isolate_failures=True)
    before_cleanup = hashes(home)
    original = workspace.replace_path_with_retry

    def interrupt(source, target):
        if source == bad:
            if after_move:
                original(source, target)
            raise OSError("interrupted directory removal")
        original(source, target)

    monkeypatch.setattr(workspace, "replace_path_with_retry", interrupt)
    with pytest.raises(UpgradeError):
        upgrade(home, isolate_failures=True, discard=confirmed(report))
    monkeypatch.setattr(workspace, "replace_path_with_retry", original)
    upgrade(home, restore=True)
    assert hashes(home) == before_cleanup
    assert (
        loads((home / "sessions/ses_fixture/context.json").read_text("utf-8"))["schema_version"]
        == 4
    )


@pytest.mark.parametrize("historical_home", ["sqlite"], indirect=True)
def test_unversioned_task_database_is_one_boundary_and_keeps_other_domains(historical_home):
    home = historical_home
    with closing(sqlite3.connect(home / "tasks.sqlite3")) as connection:
        connection.execute("UPDATE tasks SET payload='{}'")
        connection.commit()
    options = dict(session_backend="sqlite", task_backend="sqlite", isolate_failures=True)
    before = (home / "tasks.sqlite3").read_bytes()
    report = upgrade(home, **options)
    assert confirmed(report) == {UpgradeUnit("task_store", "sqlite")}
    assert (home / "tasks.sqlite3").read_bytes() == before
    sessions = (home / "sessions.sqlite3").read_bytes()
    result = upgrade(home, **options, discard=confirmed(report))
    assert result["status"] == "upgraded"
    assert not (home / "tasks.sqlite3").exists()
    assert (home / "sessions.sqlite3").read_bytes() == sessions


def test_usage_failure_keeps_other_units_and_removes_only_ledger_on_confirmation(historical_home):
    home = historical_home
    path = home / "billing/usage.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE entry_records SET payload='{}'")
        connection.commit()
    report = upgrade(home, isolate_failures=True)
    assert confirmed(report) == {UpgradeUnit("usage", "billing/usage.sqlite3")}
    before = hashes(home)
    result = upgrade(home, isolate_failures=True, discard=confirmed(report))
    assert result["status"] == "upgraded"
    assert not path.exists()
    assert hashes(home) == {
        name: checksum for name, checksum in before.items() if name != "billing/usage.sqlite3"
    }


@pytest.mark.parametrize("historical_home", ["sqlite"], indirect=True)
def test_structural_database_error_blocks_deletion_but_allows_other_domains(historical_home):
    home = historical_home
    path = home / "sessions.sqlite3"
    path.write_bytes(b"unreadable database")
    report = upgrade(home, session_backend="sqlite", task_backend="sqlite", isolate_failures=True)
    assert report["status"] == "partial"
    assert report["failures"][0]["unit"] == {"kind": "session_store", "identity": "sqlite"}
    assert confirmed(report) == set()
    assert path.read_bytes() == b"unreadable database"
    assert "tasks.sqlite3" in report["files"]
    assert "billing/usage.sqlite3" in report["files"]


@pytest.mark.parametrize(
    "filename,content", [("context.json", b"\xff"), ("resources/index.json", b'{"resources":null}')]
)
def test_malformed_record_data_is_isolated_without_leaking_contents(
    historical_home, filename, content
):
    home = historical_home
    bad = clone_files(home, "sessions/ses_fixture", "ses_bad")
    (bad / filename).write_bytes(content)
    before = hashes(home)
    report = upgrade(home, isolate_failures=True, check=True)
    assert hashes(home) == before
    assert report["status"] == "partial"
    assert confirmed(report) == {UpgradeUnit("session", "ses_bad")}


@pytest.mark.parametrize("case", ["missing-session", "pending-commit"])
def test_unowned_or_unfinished_session_is_not_discardable(historical_home, case):
    home = historical_home
    bad = clone_files(home, "sessions/ses_fixture", "ses_bad")
    if case == "missing-session":
        (bad / "session.json").unlink()
    else:
        (bad / "commit.json").write_bytes(b"pending source publication")
    report = upgrade(
        home, isolate_failures=True, discard=frozenset({UpgradeUnit("session", "ses_bad")})
    )
    assert report["status"] == "partial"
    assert confirmed(report) == set()
    assert bad.exists()
    assert "sessions/ses_fixture/context.json" in report["files"]


def test_directory_restore_validates_all_backups_before_changing_any_data(
    historical_home, monkeypatch
):
    home = historical_home
    bad = clone_files(home, "sessions/ses_fixture", "ses_bad")
    (bad / "context.json").write_bytes(b'{"schema_version":999}')
    report = upgrade(home, isolate_failures=True)
    original = workspace.replace_path_with_retry

    def interrupt(source, target):
        original(source, target)
        if source == bad:
            (target / "context.json").write_bytes(b"damaged backup")
            raise OSError("interrupted")

    monkeypatch.setattr(workspace, "replace_path_with_retry", interrupt)
    with pytest.raises(UpgradeError):
        upgrade(home, isolate_failures=True, discard=confirmed(report))
    monkeypatch.setattr(workspace, "replace_path_with_retry", original)
    before_restore = hashes(home)
    with pytest.raises(UpgradeError, match="damaged backup"):
        upgrade(home, restore=True)
    assert hashes(home) == before_restore


def test_directory_restore_can_resume_after_copy_interruption(historical_home, monkeypatch):
    home = historical_home
    bad = clone_files(home, "sessions/ses_fixture", "ses_bad")
    (bad / "context.json").write_bytes(b'{"schema_version":999}')
    report = upgrade(home, isolate_failures=True)
    before = hashes(home)
    original_move = workspace.replace_path_with_retry

    def interrupt_move(source, target):
        original_move(source, target)
        if source == bad:
            raise OSError("interrupted")

    monkeypatch.setattr(workspace, "replace_path_with_retry", interrupt_move)
    with pytest.raises(UpgradeError):
        upgrade(home, isolate_failures=True, discard=confirmed(report))
    monkeypatch.setattr(workspace, "replace_path_with_retry", original_move)
    original_copy = workspace.shutil.copytree

    def interrupt_copy(source, target, *args, **kwargs):
        if target == bad:
            target.mkdir()
            (target / "partial.txt").write_bytes(b"partial restore")
            raise OSError("interrupted restore")
        return original_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(workspace.shutil, "copytree", interrupt_copy)
    with pytest.raises(UpgradeError):
        upgrade(home, restore=True)
    monkeypatch.setattr(workspace.shutil, "copytree", original_copy)
    upgrade(home, restore=True)
    assert hashes(home) == before


@pytest.mark.parametrize("historical_home", ["sqlite"], indirect=True)
def test_task_database_cleanup_includes_wal_and_preserves_unselected_files(historical_home):
    home = historical_home
    database = home / "tasks.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("UPDATE tasks SET payload='{}'")
        connection.commit()
        snapshots = {
            suffix: database.with_name(database.name + suffix).read_bytes()
            for suffix in ("", "-wal", "-shm")
        }
    for suffix, data in snapshots.items():
        database.with_name(database.name + suffix).write_bytes(data)
    (home / "tasks").mkdir()
    unselected = home / "tasks/private.json"
    unselected.write_bytes(b"unselected backend")
    options = dict(session_backend="sqlite", task_backend="sqlite", isolate_failures=True)
    report = upgrade(home, **options)
    result = upgrade(home, **options, discard=confirmed(report))
    assert result["status"] == "upgraded"
    assert not any(database.with_name(database.name + suffix).exists() for suffix in snapshots)
    assert unselected.read_bytes() == b"unselected backend"
