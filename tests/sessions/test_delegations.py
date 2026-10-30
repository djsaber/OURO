from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

import ouro.sessions.backends.files.commits as file_commits
from ouro.core.agents import (
    AgentDefinition,
    DelegatedConstraints,
    DelegationRecord,
    DelegationResult,
    ResultDelivery,
    ToolSelection,
)
from ouro.core.errors import (
    DelegationRevisionConflictError,
    PersistenceError,
    SessionRecoveryRequiredError,
)
from ouro.core.messages import MessageRecord
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import LocalSessionStore, SessionStore, SQLiteSessionStore


@pytest.fixture(params=("files", "sqlite"))
def delegation_store(request: pytest.FixtureRequest, tmp_path) -> Iterator[SessionStore]:
    store: SessionStore
    if request.param == "files":
        store = LocalSessionStore(tmp_path / "files")
    else:
        store = SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")
    store.initialize()
    yield store


def _record(root: SessionRecord, *, suffix: str = "one") -> DelegationRecord:
    definition = AgentDefinition(
        agent_id="general",
        name="General",
        description="General subagent.",
        tool_selection=ToolSelection(mode="selected", names=()),
    )
    return DelegationRecord(
        delegation_id=f"dlg_{suffix}",
        idempotency_key=f"host/{suffix}",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        parent_run_id="run_parent",
        agent_id=definition.agent_id,
        task_description=f"Task {suffix}",
        prompt_digest=f"sha256:{suffix}",
        definition=definition,
    )


def test_delegation_queries_never_enter_mutation(delegation_store, monkeypatch):
    root = SessionRecord()
    delegation_store.create_session(root)
    record = _record(root)
    delegation_store.create_delegation(record)

    def no_mutation(*args, **kwargs):
        raise AssertionError("Delegation query entered mutation")

    monkeypatch.setattr(delegation_store, "session_mutation", no_mutation)
    assert delegation_store.load_delegation(root.session_id, record.delegation_id) == record
    assert delegation_store.load_delegation_by_id(record.delegation_id) == record
    assert delegation_store.list_delegations(root.session_id) == [record]
    assert (
        delegation_store.find_delegation_by_idempotency_key(root.session_id, record.idempotency_key)
        == record
    )
    assert delegation_store.find_delegation_by_child_session(root.session_id, "ses_missing") is None


def test_file_delegation_queries_leave_pending_decision_for_explicit_recovery(
    tmp_path, monkeypatch
):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    root = SessionRecord()
    store.create_session(root)
    record = store.create_delegation(_record(root))

    def fail_head(connection, session):
        raise OSError("count installation interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(file_commits, "write_session", fail_head)
        with pytest.raises(PersistenceError, match="count installation interrupted"):
            store.commit_session(
                SessionCommit(
                    before=root,
                    runs=(RunRecord(session_id=root.session_id, model_round_count=1),),
                    steps=(
                        SessionCommitStep(
                            message=MessageRecord.text(
                                session_id=root.session_id,
                                role="user",
                                text="external message fact",
                            )
                        ),
                    ),
                )
            )
    pending = store.sessions_dir / root.session_id / "commit.json"
    accepted = pending.read_bytes()
    cold = LocalSessionStore(tmp_path)
    cold.initialize()
    for query in (
        lambda: cold.load_delegation(root.session_id, record.delegation_id),
        lambda: cold.load_delegation_by_id(record.delegation_id),
        lambda: cold.list_delegations(root.session_id),
        lambda: cold.find_delegation_by_idempotency_key(root.session_id, record.idempotency_key),
        lambda: cold.find_delegation_by_child_session(root.session_id, "ses_missing"),
    ):
        with pytest.raises(SessionRecoveryRequiredError):
            query()
        assert pending.read_bytes() == accepted
    with cold.session_mutation(root.session_id):
        pass
    assert not pending.exists()
    assert cold.load_delegation(root.session_id, record.delegation_id) == record
    assert cold.load_run_request_counts(root.session_id)[0].model_round_count == 1


def test_delegation_repository_round_trip_and_revision(
    delegation_store: SessionStore,
) -> None:
    root = SessionRecord(session_id="ses_root")
    delegation_store.create_session(root)
    record = _record(root)

    assert delegation_store.create_delegation(record) == record
    assert delegation_store.load_delegation(root.session_id, record.delegation_id) == record
    assert delegation_store.load_delegation_by_id(record.delegation_id) == record
    assert (
        delegation_store.find_delegation_by_idempotency_key(root.session_id, record.idempotency_key)
        == record
    )

    child = SessionRecord(
        session_id="ses_child",
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id=record.delegation_id,
    )
    delegation_store.create_session(child)
    updated = record.model_copy(
        update={
            "spawn_state": "child_created",
            "child_session_id": child.session_id,
        }
    )
    saved = delegation_store.save_delegation(updated, expected_revision=0)

    assert saved.revision == 1
    assert (
        delegation_store.find_delegation_by_child_session(
            root.session_id,
            child.session_id,
        )
        == saved
    )
    assert delegation_store.list_delegations(root.session_id) == [saved]
    with pytest.raises(DelegationRevisionConflictError, match="revision conflict"):
        delegation_store.save_delegation(saved, expected_revision=0)


def test_delegation_repository_enforces_idempotency_key(
    delegation_store: SessionStore,
) -> None:
    root = SessionRecord(session_id="ses_root")
    delegation_store.create_session(root)
    first = _record(root, suffix="same")
    delegation_store.create_delegation(first)

    duplicate = first.model_copy(update={"delegation_id": "dlg_other"})
    with pytest.raises(PersistenceError, match="idempotency"):
        delegation_store.create_delegation(duplicate)


def test_delegation_repository_lists_one_root(
    delegation_store: SessionStore,
) -> None:
    root = SessionRecord(session_id="ses_root")
    delegation_store.create_session(root)
    incomplete = _record(root, suffix="incomplete")
    delegation_store.create_delegation(incomplete)

    child = SessionRecord(
        session_id="ses_child",
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id="dlg_ready",
    )
    delegation_store.create_session(child)
    ready = _record(root, suffix="ready").model_copy(
        update={
            "child_session_id": child.session_id,
            "current_child_run_id": "run_child",
            "spawn_state": "submitted",
            "result": DelegationResult(
                child_session_id=child.session_id,
                child_run_id="run_child",
                terminal_status="completed",
                delivery=ResultDelivery(policy="next_boundary"),
            ),
        }
    )
    delegation_store.create_delegation(ready)

    assert delegation_store.list_delegations(root.session_id) == [incomplete, ready]


@pytest.mark.parametrize("old_count", [4, 128])
def test_delegation_lookups_ignore_other_parent_runs(delegation_store, monkeypatch, old_count):
    store = delegation_store
    root = SessionRecord()
    store.create_session(root)
    for index in range(old_count):
        old = _record(root, suffix=f"old_{index}").model_copy(
            update={"parent_run_id": f"run_old_{index}"}
        )
        store.create_delegation(old)
        if isinstance(store, LocalSessionStore):
            path = (
                store.sessions_dir / root.session_id / "delegations" / f"{old.delegation_id}.json"
            )
            path.write_text("invalid unrelated body", encoding="utf-8")
        else:
            with closing(sqlite3.connect(store.database)) as connection, connection:
                connection.execute(
                    "UPDATE delegations SET payload = ? WHERE delegation_id = ?",
                    ("invalid unrelated body", old.delegation_id),
                )
    target = _record(root).model_copy(
        update={"child_session_id": "ses_child", "idempotency_key": "宿主/请求:?\\opaque"}
    )
    store.create_delegation(target)
    reads = []
    opened = Path.open

    def open_record(path, *args, **kwargs):
        if path.parent.name == "delegations":
            assert path.stem == target.delegation_id
            reads.append(path.stem)
        return opened(path, *args, **kwargs)

    if isinstance(store, LocalSessionStore):
        monkeypatch.setattr(Path, "open", open_record)
    instructions = 0

    def progress():
        nonlocal instructions
        instructions += 1
        return 0

    with store.session_read(root.session_id) as connection:
        if isinstance(store, SQLiteSessionStore):
            connection.set_progress_handler(progress, 1)
        try:
            assert store.list_delegations(
                root.session_id,
                parent_session_id=root.session_id,
                parent_run_id=target.parent_run_id,
            ) == [target]
            assert store.find_delegation_by_child_session(root.session_id, "ses_child") == target
            assert (
                store.find_delegation_by_idempotency_key(root.session_id, target.idempotency_key)
                == target
            )
            assert store.find_delegation_by_idempotency_key(root.session_id, "absent") is None
            assert store.list_delegations(root.session_id, parent_run_id="run_absent") == []
        finally:
            if isinstance(store, SQLiteSessionStore):
                connection.set_progress_handler(None, 0)
    if isinstance(store, LocalSessionStore):
        assert len(reads) == 3
    else:
        # Includes five queries and their Session guards, independent of old_count.
        assert instructions < 1000


def test_delegation_updates_replace_lookup_keys(delegation_store):
    store = delegation_store
    root = SessionRecord()
    store.create_session(root)
    original = store.create_delegation(
        _record(root).model_copy(update={"child_session_id": "ses_old"})
    )
    updated = store.save_delegation(
        original.model_copy(
            update={
                "parent_run_id": "run_other",
                "child_session_id": "ses_new",
                "idempotency_key": "different opaque key",
            }
        ),
        expected_revision=0,
    )
    assert store.list_delegations(root.session_id, parent_run_id=original.parent_run_id) == []
    assert store.find_delegation_by_child_session(root.session_id, "ses_old") is None
    assert (
        store.find_delegation_by_idempotency_key(root.session_id, original.idempotency_key) is None
    )
    assert store.list_delegations(root.session_id, parent_run_id="run_other") == [updated]
    assert store.find_delegation_by_child_session(root.session_id, "ses_new") == updated
    assert (
        store.find_delegation_by_idempotency_key(root.session_id, updated.idempotency_key)
        == updated
    )
    duplicate = _record(root, suffix="duplicate").model_copy(update={"child_session_id": "ses_new"})
    with pytest.raises(PersistenceError):
        store.create_delegation(duplicate)
    assert store.list_delegations(root.session_id) == [updated]


@pytest.mark.parametrize("operation", ["create", "update"])
def test_delegation_native_decision_recovers_record_and_lookups(
    delegation_store, monkeypatch, operation
):
    store = delegation_store
    root = SessionRecord()
    store.create_session(root)
    record = _record(root)
    if operation == "update":
        store.create_delegation(record)
    changed = record.model_copy(
        update={"child_session_id": "ses_child", "parent_run_id": "run_new"}
    )

    def publish(repository):
        if operation == "create":
            return repository.create_delegation(changed)
        return repository.save_delegation(changed, expected_revision=0)

    if isinstance(store, LocalSessionStore):
        replace = os.replace

        def fail_child(source, destination):
            if Path(destination).parent.name == "children":
                raise OSError("child lookup installation interrupted")
            return replace(source, destination)

        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", fail_child)
            with pytest.raises(PersistenceError, match="child lookup installation interrupted"):
                publish(store)
        cold = LocalSessionStore(store.root)
        cold.initialize()
        pending = cold.sessions_dir / root.session_id / "commit.json"
        accepted = pending.read_bytes()
        for query in (
            lambda: cold.load_delegation(root.session_id, record.delegation_id),
            lambda: cold.list_delegations(root.session_id, parent_run_id="run_new"),
            lambda: cold.find_delegation_by_child_session(root.session_id, "ses_child"),
            lambda: cold.find_delegation_by_idempotency_key(
                root.session_id, record.idempotency_key
            ),
        ):
            with pytest.raises(SessionRecoveryRequiredError):
                query()
            assert pending.read_bytes() == accepted
        with cold.session_mutation(root.session_id):
            pass
        assert not pending.exists()
    else:
        action = "INSERT" if operation == "create" else "UPDATE"
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute(
                f"CREATE TRIGGER fail_delegation AFTER {action} ON delegations "
                "BEGIN SELECT RAISE(ABORT, 'Delegation transaction failed'); END"
            )
        with pytest.raises((PersistenceError, sqlite3.IntegrityError)):
            publish(store)
        cold = SQLiteSessionStore(store.database)
        cold.initialize()
        assert cold.list_delegations(root.session_id) == ([record] if operation == "update" else [])
        assert cold.list_delegations(root.session_id, parent_run_id="run_new") == []
        assert cold.find_delegation_by_child_session(root.session_id, "ses_child") is None
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute("DROP TRIGGER fail_delegation")
        publish(cold)
    with cold.session_mutation(root.session_id):
        pass
    durable = cold.load_delegation(root.session_id, record.delegation_id)
    assert durable.revision == (1 if operation == "update" else 0)
    assert cold.list_delegations(root.session_id, parent_run_id="run_parent") == []
    assert cold.list_delegations(root.session_id, parent_run_id="run_new") == [durable]
    assert cold.find_delegation_by_child_session(root.session_id, "ses_child") == durable
    assert (
        cold.find_delegation_by_idempotency_key(root.session_id, record.idempotency_key) == durable
    )
    with pytest.raises(PersistenceError, match="idempotency"):
        cold.create_delegation(changed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (
            "definition",
            AgentDefinition(
                agent_id="general", name="Changed", description="Replace frozen instructions."
            ),
        ),
        ("constraints", DelegatedConstraints(allow_external_paths=True)),
        ("approval_mode", "auto"),
        ("interaction_mode", "unattended"),
        ("budget", RunBudgetPolicy(max_model_rounds=10)),
    ],
)
def test_delegation_lifecycle_revision_cannot_replace_frozen_authority(
    delegation_store: SessionStore, field, value
) -> None:
    root = SessionRecord()
    delegation_store.create_session(root)
    original = delegation_store.create_delegation(_record(root))
    with pytest.raises(PersistenceError, match=f"frozen contract cannot change: {field}"):
        delegation_store.save_delegation(
            original.model_copy(update={field: value}), expected_revision=original.revision
        )
    assert delegation_store.load_delegation(root.session_id, original.delegation_id) == original
    closed = delegation_store.save_delegation(
        original.model_copy(update={"control_state": "closed"}), expected_revision=0
    )
    assert closed.revision == 1
    assert closed.control_state == "closed"
