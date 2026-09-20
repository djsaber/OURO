import pytest

from ouro.application import SessionService, SessionSummary
from ouro.core.errors import PersistenceError, SessionNotFoundError
from ouro.local_application import build_local_session_services
from tests.session_summaries import add_session_summaries


@pytest.fixture(params=["files", "sqlite"])
def services(tmp_path, request):
    return build_local_session_services(tmp_path, session_backend=request.param)


def test_summaries_return_all_retained_results_and_the_actual_active_pointer(services):
    store, sessions = services._store, services.sessions
    session = sessions.create(current_model="deleted/model").session
    records = add_session_summaries(store, session.session_id)
    # The newest retained result is not necessarily the active one after history edits.
    store.activate_compaction(session.session_id, records[0].compaction_id)
    before = store.load_session(session.session_id)

    summaries = SessionService(store).summaries(session.session_id)

    assert all(isinstance(item, SessionSummary) for item in summaries)
    assert [item.compaction_id for item in summaries] == [item.compaction_id for item in records]
    assert [item.summary for item in summaries] == [item.summary for item in records]
    assert [item.active for item in summaries] == [True, False]
    assert [item.trigger for item in summaries] == ["manual", "auto"]
    assert [item.created_at for item in summaries] == [item.created_at for item in records]
    assert all(item.session_id == session.session_id for item in summaries)
    assert store.load_session(session.session_id) == before


def test_summaries_distinguish_an_empty_session_from_a_missing_session(services):
    session = services.sessions.create().session
    assert services.sessions.summaries(session.session_id) == ()
    with pytest.raises(SessionNotFoundError, match="session not found"):
        services.sessions.summaries("ses_missing")


def test_summaries_do_not_read_transcripts_or_recover_running_sessions(services, monkeypatch):
    store, sessions = services._store, services.sessions
    session = sessions.create().session
    records = add_session_summaries(store, session.session_id)
    before = store.load_session_record(session.session_id).model_copy(
        update={"status": "running", "active_run_id": "run_unavailable"}
    )
    store.save_session(before)

    def unexpected_read(*args, **kwargs):
        raise AssertionError("Summary queries must not load or recover execution history")

    for method in ("load_session", "load_runtime_session", "load_messages_page", "load_runs"):
        monkeypatch.setattr(store, method, unexpected_read)

    assert [item.summary for item in sessions.summaries(session.session_id)] == [
        item.summary for item in records
    ]
    assert store.load_session_record(session.session_id) == before


def test_summaries_reject_missing_active_compaction(services):
    store, sessions = services._store, services.sessions
    session = sessions.create().session
    record = store.load_session_record(session.session_id)
    store.save_session(record.model_copy(update={"active_compaction_id": "cmp_missing"}))

    with pytest.raises(PersistenceError, match="missing compaction cmp_missing"):
        sessions.summaries(session.session_id)


def test_summaries_propagate_persistence_errors(services, monkeypatch):
    session = services.sessions.create().session

    def unreadable(_session_id):
        raise PersistenceError("invalid compaction record")

    monkeypatch.setattr(services._store, "load_compactions", unreadable)
    with pytest.raises(PersistenceError, match="invalid compaction record"):
        services.sessions.summaries(session.session_id)
