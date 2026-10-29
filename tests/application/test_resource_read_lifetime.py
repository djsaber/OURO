from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

import ouro.sessions.backends.files.commits as file_commits
from ouro.application.resources import ResourceService
from ouro.application.sessions.service import SessionService
from ouro.application.transfer.sessions import SessionTransferService
from ouro.core.errors import PersistenceError, SessionBusyError, SessionRecoveryRequiredError
from ouro.core.messages import MessageRecord
from ouro.local_locks import FileMutationLock
from ouro.resources import LocalResourceStore
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


@pytest.fixture(params=["files", "sqlite"])
def services(tmp_path, request):
    store = (
        LocalSessionStore(tmp_path / "home")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "home" / "sessions.db")
    )
    store.initialize()
    files = LocalResourceStore(tmp_path / "home")
    files.initialize()
    resources = ResourceService(store=files, sessions=store)
    sessions = SessionService(store, resources=resources)
    session = sessions.create(session_id="ses_selected").session
    source = tmp_path / "attachment.txt"
    source.write_bytes(b"complete exported content")
    resource = resources.import_resource(session.session_id, source).resource
    return store, files, resources, sessions, session, resource


def test_export_and_resource_catalog_do_not_enter_mutation(services, monkeypatch):
    store, files, resources, _, session, resource = services

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only resource/export query entered recovery or mutation")

    monkeypatch.setattr(store, "session_mutation", forbidden)
    assert store.load_harness(session.session_id).session_id == session.session_id
    assert (
        resources.list_resources(session.session_id).resources[0].resource_id
        == resource.resource_id
    )
    archive = SessionTransferService(store, files).export((session.session_id,))
    assert list(archive.files.values()) == [b"complete exported content"]
    assert not store.is_session_locked(session.session_id)


def test_export_owns_resource_closure_without_blocking_other_sessions(services, monkeypatch):
    store, files, resources, sessions, session, resource = services
    other = sessions.create(session_id="ses_other").session
    reading = Event()
    release = Event()
    load = files.load_content

    def held_read(*args, **kwargs):
        reading.set()
        assert release.wait(timeout=10)
        return load(*args, **kwargs)

    monkeypatch.setattr(files, "load_content", held_read)
    with ThreadPoolExecutor(max_workers=2) as executor:
        export = executor.submit(SessionTransferService(store, files).export, (session.session_id,))
        try:
            assert reading.wait(timeout=10)
            with pytest.raises(SessionBusyError):
                resources.remove_resource(session.session_id, resource.resource_id)
            message = MessageRecord.text(session_id=other.session_id, role="user", text="unrelated")
            executor.submit(store.append_message, message).result(timeout=10)
        finally:
            release.set()
        archive = export.result(timeout=10)
    # The archive owns actual bytes after releasing its synchronous read boundary.
    assert resources.remove_resource(session.session_id, resource.resource_id).content_collected
    assert list(archive.files.values()) == [b"complete exported content"]
    assert not store.is_session_locked(session.session_id)


def test_files_resource_list_reports_pending_decision_without_recovery(tmp_path, monkeypatch):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    sessions = SessionService(store)
    selected = sessions.create(session_id="ses_pending").session
    other = sessions.create(session_id="ses_other").session

    def stop_before_head(connection, session):
        raise OSError("head installation interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(file_commits, "write_session", stop_before_head)
        with pytest.raises(PersistenceError, match="installation interrupted"):
            store.append_message(
                MessageRecord.text(session_id=selected.session_id, role="user", text="accepted")
            )
    pending = store.sessions_dir / selected.session_id / "commit.json"
    decision = pending.read_bytes()
    for query in (
        store.load_resource_references,
        store.load_harness,
    ):
        with pytest.raises(SessionRecoveryRequiredError):
            query(selected.session_id)
        assert pending.read_bytes() == decision
    assert store.load_resource_references(other.session_id) == []
    with store.session_mutation(selected.session_id):
        pass
    assert store.load_resource_references(selected.session_id) == []
    assert not pending.exists()


@pytest.mark.parametrize("operation", ["show", "export", "project"])
def test_selected_resource_read_finishes_before_concurrent_delete(
    services, tmp_path, monkeypatch, operation
):
    store, files, resources, sessions, session, resource = services
    other = sessions.create().session
    blob = resources.resource_path(session.session_id, resource.resource_id)
    entered, release, waiting = Event(), Event(), Event()
    selected_record = resources._record
    acquire = FileMutationLock.acquire

    def record(session_id, resource_id):
        value = selected_record(session_id, resource_id)
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return value

    def wait_lock(lock, *args, **kwargs):
        if entered.is_set() and not release.is_set():
            waiting.set()
        return acquire(lock, *args, **kwargs)

    def read():
        if operation == "show":
            return resources.show_resource(session.session_id, resource.resource_id).resource_id
        if operation == "export":
            output = tmp_path / "exported.txt"
            resources.export_resource(session.session_id, resource.resource_id, output)
            return output.read_bytes()
        return resources.project_model_messages(
            [
                MessageRecord(
                    session_id=session.session_id, role="user", resource_refs=[resource.resource_id]
                )
            ],
            input_modalities=frozenset(),
        )[0].text_content

    monkeypatch.setattr(resources, "_record", record)
    monkeypatch.setattr(FileMutationLock, "acquire", wait_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(read)
        deleting = None
        try:
            assert entered.wait(10)
            deleting = pool.submit(
                resources.remove_resource, session.session_id, resource.resource_id
            )
            assert waiting.wait(10)
            store.append_message(
                MessageRecord.text(session_id=other.session_id, role="user", text="independent")
            )
            assert not deleting.done()
        finally:
            release.set()
        result = reading.result(timeout=10)
        assert deleting is not None
        deleting.result(timeout=10)
    assert not blob.exists()
    if operation == "show":
        assert result == resource.resource_id
    elif operation == "export":
        assert result == b"complete exported content"
    else:
        assert "complete exported content" in result
