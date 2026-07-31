from __future__ import annotations

import pytest

from ouro.core.agents import AgentDefinition, DelegationRecord
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.resources import SessionResourceReference
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import (
    LocalSessionStore,
    SQLiteSessionStore,
    import_sessions_to_sqlite,
)


def test_import_sessions_to_sqlite_is_exact_and_verified(tmp_path) -> None:
    source = LocalSessionStore(tmp_path / "files")
    source.initialize()
    first = SessionRecord(title="First")
    second = SessionRecord(title="Second")
    message = MessageRecord.text(
        session_id=first.session_id,
        role="user",
        text="hello",
        sequence=1,
    )
    run = RunRecord(session_id=first.session_id, status="completed")
    reference = SessionResourceReference(resource_id="res_" + "c" * 64)
    source.create_session_snapshot(first, [message], [reference])
    source.save_session_extension_state(
        SessionExtensionStateRecord(
            session_id=first.session_id,
            owner_id="dev.ouro.goal_mode",
            state_schema_version=2,
            payload={"status": "active"},
        )
    )
    source.save_run(run)
    source.append_ordered_event(
        EventRecord(
            session_id=first.session_id,
            run_id=run.run_id,
            event_type="run_completed",
        )
    )
    source.create_session(second)
    delegation = DelegationRecord(
        idempotency_key="migration/delegation",
        root_session_id=first.session_id,
        parent_session_id=first.session_id,
        parent_run_id=run.run_id,
        agent_id="reader",
        task_description="Verify migration",
        prompt_digest="sha256:" + "a" * 64,
        definition=AgentDefinition(
            agent_id="reader",
            name="Reader",
            description="Read evidence.",
        ),
    )
    source.create_delegation(delegation)
    target = SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")

    report = import_sessions_to_sqlite(source, target)

    assert report.session_ids == sorted([first.session_id, second.session_id])
    assert report.message_count == 1
    assert report.event_count == 1
    assert report.run_count == 1
    assert report.resource_reference_count == 1
    assert report.extension_state_count == 1
    assert report.delegation_count == 1
    assert target.list_delegations(first.session_id) == [delegation]
    for session in source.list_sessions():
        assert target.load_session(session.session_id) == source.load_session(session.session_id)
        assert target.load_resource_references(
            session.session_id
        ) == source.load_resource_references(session.session_id)


def test_import_sessions_to_sqlite_rolls_back_every_session_on_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = LocalSessionStore(tmp_path / "files")
    source.initialize()
    first = SessionRecord(session_id="session_first", title="First")
    second = SessionRecord(session_id="session_second", title="Second")
    source.create_session(first)
    source.create_session(second)
    target = SQLiteSessionStore(tmp_path / "sqlite" / "ouro.db")
    original = target.create_session_snapshot

    def fail_on_second(*args, **kwargs) -> None:
        if args[0].session_id == second.session_id:
            raise RuntimeError("injected copy failure")
        original(*args, **kwargs)

    monkeypatch.setattr(target, "create_session_snapshot", fail_on_second)

    with pytest.raises(Exception, match="rolled back"):
        import_sessions_to_sqlite(source, target)

    assert target.list_sessions() == []
