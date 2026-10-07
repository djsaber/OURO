from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from ouro.automation.local_files import LocalTaskRepository
from ouro.automation.models import (
    TaskConfiguration,
    TaskConflictError,
    TaskExecution,
    TaskRecord,
    TaskSchedule,
)
from ouro.automation.sqlite import SQLiteTaskRepository
from ouro.core.errors import PersistenceError

NOW = datetime(2026, 9, 7, 9, tzinfo=UTC)
REPOSITORIES = {"files": LocalTaskRepository, "sqlite": SQLiteTaskRepository}


@pytest.fixture(params=REPOSITORIES)
def repository(tmp_path, request):
    repository = REPOSITORIES[request.param](tmp_path)
    repository.initialize()
    return repository


def task_record(home, *, task_id="task_one", **changes):
    return TaskRecord(
        task_id=task_id,
        configuration=TaskConfiguration(
            description="Scheduled work",
            workspace=str(home),
            model="faux/task",
            schedule=TaskSchedule(frequency="hourly", starts_at=NOW),
        ),
        created_at=NOW,
        updated_at=NOW,
        next_run_at=NOW,
    ).model_copy(update=changes)


def receipt(task, number, *, status="completed"):
    return TaskExecution(
        task_id=task.task_id,
        execution_id=f"execution_{number}",
        session_id="ses_task",
        run_id=f"run_{number}",
        scheduled_at=NOW + timedelta(hours=number),
        created_at=NOW + timedelta(hours=number),
        updated_at=NOW + timedelta(hours=number),
        status=status,
        ended_at=NOW + timedelta(hours=number) if status in {"completed", "failed"} else None,
    )


def test_sqlite_concurrent_initialization_preserves_all_tasks(tmp_path):
    barrier = Barrier(8)

    def create(index):
        repository = SQLiteTaskRepository(tmp_path)
        barrier.wait(timeout=5)
        repository.initialize()
        task = task_record(tmp_path, task_id=f"task_{index}")
        repository.commit(task, expected_revision=None)
        return task

    with ThreadPoolExecutor(max_workers=8) as executor:
        created = list(executor.map(create, range(8)))
    repository = SQLiteTaskRepository(tmp_path)
    repository.initialize()
    assert {task.task_id for task in repository.list()} == {task.task_id for task in created}


def test_backends_keep_history_counts_and_atomic_revision_contract(repository, tmp_path):
    task = task_record(tmp_path)
    repository.commit(task, expected_revision=None)
    assert repository.next_due() == NOW
    assert repository.due(NOW, limit=1) == (task,)
    for index in range(5):
        execution = receipt(task, index, status="failed" if index == 3 else "completed")
        updated = task.model_copy(
            update={"revision": task.revision + 1, "last_execution_id": execution.execution_id}
        )
        repository.commit(updated, expected_revision=task.revision, execution=execution)
        task = updated
    peer = type(repository)(tmp_path)
    peer.initialize()
    assert peer.get(task.task_id) == task
    assert [entry.execution_id for entry in peer.executions(task.task_id, limit=2)] == [
        "execution_4",
        "execution_3",
    ]
    assert peer.counts().completed_executions == 4
    assert peer.counts().failed_executions == 1
    with pytest.raises(TaskConflictError):
        repository.commit(task, expected_revision=1)
    before = peer.get(task.task_id)
    with pytest.raises(ValueError, match="another task"):
        repository.commit(
            task.model_copy(update={"revision": 7}),
            expected_revision=6,
            execution=receipt(task.model_copy(update={"task_id": "task_other"}), 9),
        )
    assert peer.get(task.task_id) == before
    assert len(peer.executions(task.task_id)) == 5
    repository.delete(task.task_id, expected_revision=task.revision)
    assert peer.list() == ()
    assert peer.executions(task.task_id) == ()
    assert peer.counts().completed_executions == peer.counts().failed_executions == 0


def test_backends_reject_duplicate_occurrences_without_changing_the_task(repository, tmp_path):
    task = task_record(tmp_path)
    first = receipt(task, 1)
    repository.commit(task, expected_revision=None, execution=first)
    duplicate = first.model_copy(update={"execution_id": "execution_duplicate"})
    with pytest.raises(TaskConflictError):
        repository.commit(
            task.model_copy(update={"revision": 2}), expected_revision=1, execution=duplicate
        )
    assert repository.get(task.task_id).revision == 1
    assert repository.executions(task.task_id) == (first,)


def test_backends_order_due_pages_and_keep_active_work_exclusive(repository, tmp_path):
    for number in range(4):
        task = task_record(tmp_path, task_id=f"task_{number}")
        execution = None
        if number == 1:
            execution = receipt(task, 1, status="paused").model_copy(
                update={"awaiting_approval": True}
            )
            task = task.model_copy(
                update={"active_execution_id": execution.execution_id, "status": "paused"}
            )
        if number == 2:
            task = task.model_copy(update={"enabled": False, "status": "paused"})
        repository.commit(task, expected_revision=None, execution=execution)
    assert [task.task_id for task in repository.due(NOW, limit=1)] == ["task_0"]
    assert [task.task_id for task in repository.due(NOW, limit=1, after=(NOW, "task_0"))] == [
        "task_3"
    ]
    assert len(repository.active()) == 1
    assert repository.counts().paused == 2
    assert repository.counts().awaiting_approval == 1
    with pytest.raises(TaskConflictError):
        repository.delete("task_1", expected_revision=1)


def test_backends_keep_execution_occurrence_identity_immutable(repository, tmp_path):
    task = task_record(tmp_path)
    execution = receipt(task, 1)
    repository.commit(task, expected_revision=None, execution=execution)
    changed = execution.model_copy(update={"scheduled_at": NOW + timedelta(days=1)})
    with pytest.raises(TaskConflictError):
        repository.commit(
            task.model_copy(update={"revision": 2}), expected_revision=1, execution=changed
        )
    assert repository.get(task.task_id).revision == 1
    assert repository.executions(task.task_id) == (execution,)


def test_parallel_writers_allow_only_one_revision_winner(repository, tmp_path):
    task = task_record(tmp_path)
    repository.commit(task, expected_revision=None)

    def update(number):
        peer = type(repository)(tmp_path)
        updated = task.model_copy(update={"revision": 2, "session_id": f"ses_{number}"})
        try:
            peer.commit(updated, expected_revision=1)
            return True
        except TaskConflictError:
            return False

    with ThreadPoolExecutor(max_workers=4) as workers:
        assert sum(workers.map(update, range(4))) == 1
    assert repository.get(task.task_id).revision == 2


@pytest.mark.parametrize("failure", ["execution", "task", "index"])
def test_file_commit_recovers_after_partial_publication(tmp_path, monkeypatch, failure):
    repository = LocalTaskRepository(tmp_path)
    repository.initialize()
    task = task_record(tmp_path)
    repository.commit(task, expected_revision=None)
    execution = receipt(task, 1)
    updated = task.model_copy(update={"revision": 2, "last_execution_id": execution.execution_id})
    from ouro.automation import local_files

    original = local_files.write_model_atomic

    def failing_write(path, model):
        should_fail = (
            failure == "execution"
            and path.parent.name == "executions"
            or failure == "task"
            and path.name == "task.json"
            or failure == "index"
            and path.name == "index.json"
        )
        if should_fail:
            raise PersistenceError("Simulated interrupted publication")
        original(path, model)

    with monkeypatch.context() as patch:
        patch.setattr(local_files, "write_model_atomic", failing_write)
        with pytest.raises(PersistenceError, match="interrupted"):
            repository.commit(updated, expected_revision=1, execution=execution)
    peer = LocalTaskRepository(tmp_path)
    peer.initialize()
    assert peer.get(task.task_id) == updated
    assert peer.executions(task.task_id) == (execution,)
    assert repository.counts().completed_executions == 1
    assert not (repository.path / "transaction.json").exists()
    assert not list(tmp_path.glob("*.sqlite3"))


def test_file_counts_and_history_pages_do_not_read_unrelated_history(tmp_path, monkeypatch):
    repository = LocalTaskRepository(tmp_path)
    repository.initialize()
    task = task_record(tmp_path)
    repository.commit(task, expected_revision=None)
    for index in range(12):
        execution = receipt(task, index)
        updated = task.model_copy(update={"revision": task.revision + 1})
        repository.commit(updated, expected_revision=task.revision, execution=execution)
        task = updated
    peer = LocalTaskRepository(tmp_path)
    peer.initialize()
    original = Path.read_text
    history_reads = []

    def track_reads(path, *args, **kwargs):
        if path.parent.name == "executions":
            history_reads.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", track_reads)
    assert peer.counts().completed_executions == 12
    assert peer.next_due() == NOW
    assert len(peer.list(limit=1)) == 1
    assert not history_reads
    assert len(peer.executions(task.task_id, limit=2)) == 2
    assert history_reads == ["execution_11.json", "execution_10.json"]
    (peer.path / "index.json").unlink()
    peer.initialize()
    assert peer.counts().completed_executions == 12
    assert len(history_reads) == 2


@pytest.mark.parametrize("record_id", ["../outside", "a/b", "C:\\outside", "..", "/absolute"])
def test_file_backend_rejects_paths_as_task_id(tmp_path, record_id):
    repository = LocalTaskRepository(tmp_path)
    repository.initialize()
    with pytest.raises(ValueError, match="record id"):
        repository.commit(task_record(tmp_path, task_id=record_id), expected_revision=None)


def test_file_backend_rejects_unknown_layout_version(tmp_path):
    repository = LocalTaskRepository(tmp_path)
    repository.initialize()
    (repository.path / "layout.json").write_text('{"schema_version": 999}', encoding="utf-8")
    with pytest.raises(PersistenceError, match="layout.json"):
        repository.initialize()


@pytest.mark.parametrize("delete", [False, True])
def test_file_recovery_remains_consistent_without_catalog(tmp_path, monkeypatch, delete):
    from ouro.automation import local_files

    repository = LocalTaskRepository(tmp_path)
    repository.initialize()
    task = task_record(tmp_path)
    repository.commit(task, expected_revision=None)
    original = local_files.write_model_atomic

    def interrupted_index(path, model):
        if path.name == "index.json":
            raise PersistenceError("Interrupted index publication")
        original(path, model)

    with monkeypatch.context() as patch:
        patch.setattr(local_files, "write_model_atomic", interrupted_index)
        with pytest.raises(PersistenceError, match="Interrupted"):
            if delete:
                repository.delete(task.task_id, expected_revision=1)
            else:
                repository.commit(
                    task.model_copy(update={"revision": 2}),
                    expected_revision=1,
                    execution=receipt(task, 1),
                )
    (repository.path / "index.json").unlink()
    peer = LocalTaskRepository(tmp_path)
    peer.initialize()
    if delete:
        assert peer.list() == ()
        assert peer.executions(task.task_id) == ()
        assert peer.counts().completed_executions == 0
    else:
        assert peer.get(task.task_id).revision == 2
        assert peer.counts().completed_executions == 1
        assert peer.executions(task.task_id) == (receipt(task, 1),)


def test_corrupt_file_index_rebuilds_but_corrupt_task_fails_closed(tmp_path):
    repository = LocalTaskRepository(tmp_path)
    repository.initialize()
    task = task_record(tmp_path)
    repository.commit(task, expected_revision=None)
    (repository.path / "index.json").write_text("not json", encoding="utf-8")
    assert repository.counts().waiting == 1
    task_path = repository.path / "records" / task.task_id / "task.json"
    task_path.write_text("not json", encoding="utf-8")
    with pytest.raises(PersistenceError, match="task.json"):
        repository.get(task.task_id)
    (repository.path / "index.json").unlink()
    with pytest.raises(PersistenceError, match="task.json"):
        repository.counts()


def test_task_file_lock_errors_cross_the_persistence_boundary(tmp_path, monkeypatch):
    from ouro.local_locks import FileMutationLock

    repository = LocalTaskRepository(tmp_path)
    repository.initialize()

    def denied(_self):
        raise PermissionError("Storage lock is not writable")

    monkeypatch.setattr(FileMutationLock, "acquire", denied)
    with pytest.raises(PersistenceError, match="PermissionError"):
        repository.counts()


def test_missing_task_record_is_diagnosed_during_catalog_rebuild(tmp_path):
    repository = LocalTaskRepository(tmp_path)
    repository.initialize()
    (repository.path / "records" / "task_missing").mkdir()
    (repository.path / "index.json").unlink()
    with pytest.raises(PersistenceError, match="Missing task.json"):
        repository.counts()
