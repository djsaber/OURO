import json
import sqlite3
from contextlib import closing

import pytest

from ouro.application.queued_inputs import QueuedInputQueue
from ouro.application.sessions import SessionService
from ouro.core.errors import PersistenceError, SessionBranchError
from ouro.core.harness import HarnessState
from ouro.core.messages import MessageRecord
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.session_controls import (
    HostSessionInput,
    SessionControlActivation,
    SessionControlState,
)
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore, import_sessions_to_sqlite


def build_store(path, backend):
    store = (
        LocalSessionStore(path) if backend == "files" else SQLiteSessionStore(path / "sessions.db")
    )
    store.initialize()
    return store


def state_for(session_id, value):
    return HarnessState(
        states=tuple(
            SessionExtensionStateRecord(
                session_id=session_id,
                owner_id=f"example.{policy}",
                state_schema_version=1,
                branch_policy=policy,
                payload=value,
            )
            for policy in ("copy", "reset")
        ),
        controls=SessionControlState(
            activations=[
                SessionControlActivation(
                    controller_id=f"{owner}/session_controllers/mode",
                    plugin_id=owner,
                    controller_local_id="mode",
                    plugin_class="user",
                    release_id="release",
                    generation_id="generation",
                    execution_contract_hash="contract",
                )
                for owner in ("example.copy", "example.reset")
            ]
        ),
    )


def history(store):
    session = SessionRecord()
    store.create_session(session)
    first = MessageRecord.text(session_id=session.session_id, role="user", text="first")
    second = MessageRecord.text(session_id=session.session_id, role="assistant", text="second")
    committed = store.commit_session(
        SessionCommit(
            before=session,
            steps=(
                SessionCommitStep(harness=state_for(session.session_id, 0)),
                SessionCommitStep(message=first, harness=state_for(session.session_id, 1)),
                SessionCommitStep(message=second, harness=state_for(session.session_id, 2)),
            ),
        )
    )
    return committed.session, first, second


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_branch_uses_boundary_state_and_resets_every_retained_version(tmp_path, backend):
    store = build_store(tmp_path, backend)
    session, first, _ = history(store)
    branch = SessionService(store).branch(session.session_id, first.message_id)
    snapshot = store.load_session(branch.session.session_id)
    current = store.load_harness(branch.session.session_id).state
    assert [(state.owner_id, state.payload) for state in current.states] == [("example.copy", 1)]
    assert [item.plugin_id for item in current.controls.activations] == ["example.copy"]
    source_versions = store.load_session(session.session_id).harness_versions
    assert not (
        {version.version_id for version in source_versions}
        & {version.version_id for version in snapshot.harness_versions}
    )
    source_activations = {
        item.activation_id
        for version in source_versions
        for item in version.state.controls.activations
    }
    assert current.controls.activations[0].activation_id in source_activations
    store.commit_session(
        SessionCommit(before=snapshot.session, before_message_id=snapshot.messages[0].message_id)
    )
    restored = store.load_harness(branch.session.session_id).state
    assert [(state.owner_id, state.payload) for state in restored.states] == [("example.copy", 0)]
    assert all(item.plugin_id == "example.copy" for item in restored.controls.activations)


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_branch_cannot_bypass_reject_in_a_retained_checkpoint(tmp_path, backend):
    store = build_store(tmp_path, backend)
    session, first, _ = history(store)
    rejecting = HarnessState(
        states=(
            SessionExtensionStateRecord(
                session_id=session.session_id,
                owner_id="example.reject",
                state_schema_version=1,
                branch_policy="reject",
                payload={},
            ),
        )
    )
    message = MessageRecord.text(session_id=session.session_id, role="user", text="rejection")
    store.commit_session(
        SessionCommit(
            before=session,
            steps=(
                SessionCommitStep(harness=rejecting),
                SessionCommitStep(message=message, harness=HarnessState()),
            ),
        )
    )
    assert not store.load_session_extension_states(session.session_id)
    with pytest.raises(SessionBranchError, match="example.reject"):
        SessionService(store).branch(session.session_id, message.message_id)
    SessionService(store).branch(session.session_id, first.message_id)


@pytest.mark.parametrize("source_backend", ["files", "sqlite"])
@pytest.mark.parametrize("target_backend", ["files", "sqlite"])
def test_transfer_preserves_undo_anchors_and_withdrawn_input_receipts(
    tmp_path, source_backend, target_backend
):
    source = build_store(tmp_path / "source", source_backend)
    session, first, second = history(source)
    request = HostSessionInput(host_source_id="test", queue_id="accepted", prompt="once")
    _, _, accepted = QueuedInputQueue(source).accept_host(session.session_id, request)
    cleared = accepted.model_copy(update={"metadata": {}})
    source.commit_session(
        SessionCommit(before=accepted, after=cleared, before_message_id=second.message_id)
    )
    snapshot = source.load_session(session.session_id)
    target = build_store(tmp_path / "target", target_backend)
    if target_backend == "sqlite":
        import_sessions_to_sqlite(source, target)
    else:
        target.create_session_snapshot(
            snapshot.session,
            snapshot.messages,
            harness_versions=snapshot.harness_versions,
            commits=snapshot.commits,
            compactions=snapshot.compactions,
        )
        for run in snapshot.runs:
            target.save_run(run)
        for event in snapshot.events:
            target.append_event(event)
    assert target.load_session(session.session_id) == snapshot
    with pytest.raises(ValueError, match="already accepted"):
        QueuedInputQueue(target).accept_host(session.session_id, request)
    target.commit_session(
        SessionCommit(before=snapshot.session, before_message_id=first.message_id)
    )
    assert [state.payload for state in target.load_harness(session.session_id).state.states] == [
        0,
        0,
    ]


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_snapshot_missing_checkpoint_is_not_published(tmp_path, backend):
    store = build_store(tmp_path, backend)
    session = SessionRecord()
    message = MessageRecord.text(session_id=session.session_id, role="user", text="dangling")
    message.before_harness_version = "absent"
    with pytest.raises(PersistenceError, match="missing Harness checkpoint"):
        store.create_session_snapshot(session, [message])
    assert not store.list_sessions()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_trash_keeps_undo(tmp_path, backend):
    store = build_store(tmp_path, backend)
    session, _, second = history(store)
    store.commit_session(SessionCommit(before=session, before_message_id=second.message_id))
    before = store.load_session(session.session_id)
    if backend == "files":
        source = store.sessions_dir / session.session_id
        originals = {
            path.relative_to(source): path.read_bytes()
            for path in source.rglob("*")
            if path.is_file() and path.suffix in {".json", ".jsonl"}
        }
    trash_id = store.trash_session_tree(session.session_id, (session.session_id,))
    assert not store.list_sessions()
    if backend == "files":
        target = store.trash_sessions_dir / trash_id / "sessions" / session.session_id
        assert {relative: (target / relative).read_bytes() for relative in originals} == originals
    else:
        with closing(sqlite3.connect(store.database)) as connection:
            row = connection.execute(
                "SELECT payload FROM trashed_sessions WHERE trash_id=?", (trash_id,)
            ).fetchone()
        records = json.loads(row[0])["records"]
        assert len(records["harness_versions"]["rows"]) == len(before.harness_versions)
        assert len(records["session_commits"]["rows"]) == len(before.commits)
        sessions = records["sessions"]
        restored = json.loads(sessions["rows"][0][sessions["columns"].index("payload")])
        assert restored["history_epoch"] == 1
        assert restored["harness_version"] == before.session.harness_version
