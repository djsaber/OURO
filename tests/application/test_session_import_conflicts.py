from __future__ import annotations

from pathlib import Path

import pytest

from ouro.application.data_transfer import DataTransferRequest
from ouro.core.agents import AgentDefinition, DelegationRecord
from ouro.core.errors import PersistenceError, SessionBusyError
from ouro.core.events import EventRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.local_application import build_local_data_transfer_service, build_local_session_services
from ouro.sessions.sqlite import SQLiteSessionStore
from ouro.transfer.archive import DataTransferError


def _tree(home, backend, title, child_id, *, sequence=1):
    store = build_local_session_services(home, session_backend=backend)._store
    root = SessionRecord(session_id="ses_root", title=title)
    store.create_session(root)
    run = RunRecord(session_id=root.session_id, run_id="run_root", status="completed")
    store.save_run(run)
    store.append_event(
        EventRecord(session_id=root.session_id, event_type="session_loaded", sequence=sequence)
    )
    store.append_event(
        EventRecord(
            session_id=root.session_id,
            run_id=run.run_id,
            event_type="run_completed",
            sequence=sequence + 1,
            payload={"text": title},
        )
    )
    definition = AgentDefinition(
        agent_id="review", name="Review", description="Review", system_prompt="Review"
    )
    relation = DelegationRecord(
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        parent_run_id=run.run_id,
        child_session_id=child_id,
        agent_id=definition.agent_id,
        definition=definition,
        idempotency_key="review",
        task_description="Review",
        prompt_digest="digest",
    )
    store.create_session(
        SessionRecord(
            session_id=child_id,
            session_kind="subagent",
            root_session_id=root.session_id,
            parent_session_id=root.session_id,
            delegation_id=relation.delegation_id,
            title=title,
        )
    )
    store.create_delegation(relation)
    return store


def _export(home, archive, backend):
    build_local_data_transfer_service(home, session_backend=backend).transfer(
        DataTransferRequest(action="export", kind="sessions", path=archive)
    )


def _import(home, archive, backend, replace=False):
    return build_local_data_transfer_service(home, session_backend=backend).transfer(
        DataTransferRequest(action="import", kind="sessions", path=archive, replace=replace)
    )


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_skip_and_replace_operate_on_complete_trees(tmp_path, backend):
    source, target = tmp_path / "source", tmp_path / "target"
    incoming = _tree(source, backend, "Incoming", "ses_new_child", sequence=10)
    incoming.create_session(SessionRecord(session_id="ses_fresh"))
    existing = _tree(target, backend, "Original", "ses_old_child", sequence=20)
    existing.create_session(SessionRecord(session_id="ses_unrelated"))
    original = existing.load_session("ses_root")
    unrelated = existing.load_session("ses_unrelated")
    # Prime a separate reader's cursors before replacing its on-disk history.
    existing.latest_event_sequence("ses_root")
    existing.load_events_page("ses_root", run_id="run_root")
    archive = tmp_path / "sessions.zip"
    _export(source, archive, backend)
    skipped = _import(target, archive, backend)
    assert (skipped.count, skipped.skipped, skipped.replaced) == (1, 2, 0)
    assert existing.load_session("ses_root") == original
    assert {r.session_id for r in existing.list_sessions()} == {
        "ses_root",
        "ses_old_child",
        "ses_fresh",
        "ses_unrelated",
    }
    replaced = _import(target, archive, backend, replace=True)
    assert (replaced.count, replaced.skipped, replaced.replaced) == (3, 0, 2)
    assert replaced.restart_required
    for identity in ("ses_root", "ses_new_child", "ses_fresh"):
        assert existing.load_session(identity) == incoming.load_session(identity)
    assert existing.load_session("ses_unrelated") == unrelated
    assert existing.list_delegations("ses_root") == incoming.list_delegations("ses_root")
    assert existing.latest_event_sequence("ses_root") == 11
    assert existing.load_events_page("ses_root", run_id="run_root") == incoming.load_events_page(
        "ses_root", run_id="run_root"
    )
    assert "ses_old_child" not in {r.session_id for r in existing.read_catalog().sessions}
    assert "ses_old_child" not in {r.session_id for r in existing.list_sessions()}


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("locked_id", ["ses_root", "ses_old_child"])
def test_replacement_rejects_busy_tree_but_skip_succeeds(tmp_path, backend, locked_id):
    _tree(tmp_path / "source", backend, "Incoming", "ses_new_child")
    target = _tree(tmp_path / "target", backend, "Original", "ses_old_child")
    original = target.load_session("ses_root")
    archive = tmp_path / "sessions.zip"
    _export(tmp_path / "source", archive, backend)
    lock = target.acquire_session_lock(locked_id, "running")
    assert lock is not None
    try:
        assert _import(tmp_path / "target", archive, backend).skipped == 2
        with pytest.raises(SessionBusyError):
            _import(tmp_path / "target", archive, backend, replace=True)
    finally:
        lock.release()
    assert target.load_session("ses_root") == original
    assert _import(tmp_path / "target", archive, backend, replace=True).count == 2


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_replacement_failure_restores_original_tree(tmp_path, backend, monkeypatch):
    _tree(tmp_path / "source", backend, "Incoming", "ses_new_child")
    target_home = tmp_path / "target"
    target = _tree(target_home, backend, "Original", "ses_old_child")
    before = {r.session_id: target.load_session(r.session_id) for r in target.list_sessions()}
    relations = target.list_delegations("ses_root")
    archive = tmp_path / "sessions.zip"
    _export(tmp_path / "source", archive, backend)
    if backend == "files":
        rename = Path.rename

        def fail_publish(path, destination):
            if (
                Path(destination) == target_home / "sessions" / "ses_root"
                and path.parent.name == "sessions"
                and ".session-import-" in str(path)
            ):
                raise OSError("injected replacement failure")
            return rename(path, destination)

        monkeypatch.setattr(Path, "rename", fail_publish)
    else:

        def fail_populate(store, run):
            raise PersistenceError("injected replacement failure")

        monkeypatch.setattr(SQLiteSessionStore, "save_run", fail_populate)
    with pytest.raises((PersistenceError, DataTransferError), match="injected|could not import"):
        _import(target_home, archive, backend, replace=True)
    assert {
        r.session_id: target.load_session(r.session_id) for r in target.list_sessions()
    } == before
    assert target.list_delegations("ses_root") == relations
    assert not list(target_home.glob(".session-import-*"))


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_replacement_rejects_identity_owned_by_other_tree(tmp_path, backend):
    source = build_local_session_services(tmp_path / "source", session_backend=backend)._store
    source.create_session(SessionRecord(session_id="ses_old_child"))
    target = _tree(tmp_path / "target", backend, "Original", "ses_old_child")
    archive = tmp_path / "sessions.zip"
    _export(tmp_path / "source", archive, backend)
    with pytest.raises(DataTransferError, match="different agent tree"):
        _import(tmp_path / "target", archive, backend, replace=True)
    assert len(target.list_sessions()) == 2


def test_failed_replacement_rollback_retains_original_files(tmp_path, monkeypatch):
    _tree(tmp_path / "source", "files", "Incoming", "ses_new_child")
    target_home = tmp_path / "target"
    target = _tree(target_home, "files", "Original", "ses_old_child")
    archive = tmp_path / "sessions.zip"
    _export(tmp_path / "source", archive, "files")
    rename = Path.rename

    def fail_root_publication_and_restore(path, destination):
        if Path(destination) == target_home / "sessions" / "ses_root":
            raise OSError("injected publication and rollback failure")
        return rename(path, destination)

    monkeypatch.setattr(Path, "rename", fail_root_publication_and_restore)
    with pytest.raises(PersistenceError, match="original data retained"):
        _import(target_home, archive, "files", replace=True)
    retained = list(target_home.glob(".session-import-*"))
    assert len(retained) == 1
    original = retained[0] / "backups" / "ses_root" / "session.json"
    assert SessionRecord.model_validate_json(original.read_bytes()).title == "Original"
    lock = target.acquire_session_lock("ses_root", "after-failure")
    assert lock is not None
    lock.release()
