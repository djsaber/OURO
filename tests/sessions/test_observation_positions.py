from __future__ import annotations

import pytest

from ouro.core.inputs import InputStateChange
from ouro.core.session_commits import SessionCommit, SessionCommitConflict
from ouro.core.state import RunRecord, SessionRecord
from tests.sessions.test_input_storage import accepted, repository


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    store = repository(tmp_path, request.param)
    store.initialize()
    return store


def test_commit_publishes_one_revision_and_paginates_all_records_at_that_revision(store):
    session = SessionRecord()
    store.create_session(session)
    assert store.observation_revision(session.session_id) == 0
    inputs = tuple(accepted(session, f"input_{index}") for index in range(9))
    run = RunRecord(session_id=session.session_id)
    session = store.commit_session(
        SessionCommit(before=session, inputs=inputs, runs=(run,))
    ).session
    revision = store.observation_revision(session.session_id)
    assert revision == 1
    cursor = None
    runs, states = [], []
    while True:
        page = store.load_observation_changes(
            session.session_id,
            after_revision=0,
            through_revision=revision,
            limit=3,
            cursor=cursor,
        )
        runs.extend(page.runs)
        states.extend(page.inputs)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert runs == [run]
    assert {state.input_id for state in states} == {item.item.queue_id for item in inputs}
    assert len(states) == 9
    with pytest.raises(SessionCommitConflict):
        store.commit_session(SessionCommit(before=session, inputs=(inputs[0],)))
    assert store.observation_revision(session.session_id) == revision


def test_changed_record_moves_to_next_read_boundary_without_retaining_old_versions(store):
    session = SessionRecord()
    store.create_session(session)
    inputs = tuple(accepted(session, f"input_{index}") for index in range(4))
    session = store.commit_session(SessionCommit(before=session, inputs=inputs)).session
    page = store.load_observation_changes(
        session.session_id,
        after_revision=0,
        through_revision=1,
        limit=2,
    )
    store.commit_session(
        SessionCommit(
            before=session,
            input_changes=(InputStateChange(input_id="input_3", status="cancelled"),),
        )
    )
    remainder = store.load_observation_changes(
        session.session_id,
        after_revision=0,
        through_revision=1,
        limit=2,
        cursor=page.next_cursor,
    )
    assert [state.input_id for state in remainder.inputs] == ["input_2"]
    changed = store.load_observation_changes(
        session.session_id,
        after_revision=1,
        through_revision=2,
        limit=2,
    )
    assert [(state.input_id, state.status) for state in changed.inputs] == [
        ("input_3", "cancelled")
    ]
    all_states = store.load_observation_changes(
        session.session_id,
        after_revision=0,
        through_revision=2,
        limit=10,
    )
    assert len(all_states.inputs) == 4


def test_session_storage_contains_facts_without_an_event_log(store):
    from ouro.core.events import EventRecord
    from tests.session_storage import session_connection

    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(session_id=session.session_id, status="running")
    result = store.commit_session(
        SessionCommit(
            before=session,
            runs=(run,),
            events=(EventRecord(session_id=session.session_id, event_type="run_started"),),
        )
    )
    assert result.events[0].event_type == "run_started"
    with session_connection(store, session.session_id) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "events" not in tables
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert "events" not in store.load_session(session.session_id).model_dump()
