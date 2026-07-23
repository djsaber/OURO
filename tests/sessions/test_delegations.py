from __future__ import annotations

from collections.abc import Iterator

import pytest

from ouro.core.agents import (
    AgentDefinition,
    DelegationRecord,
    DelegationResult,
    ResultDelivery,
    ToolSelection,
)
from ouro.core.errors import DelegationRevisionConflictError, PersistenceError
from ouro.core.state import SessionRecord
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
        description="General child agent.",
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


def test_delegation_repository_round_trip_and_revision(
    delegation_store: SessionStore,
) -> None:
    root = SessionRecord(session_id="ses_root")
    delegation_store.create_session(root)
    record = _record(root)

    assert delegation_store.create_delegation(record) == record
    assert delegation_store.load_delegation(root.session_id, record.delegation_id) == record
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
