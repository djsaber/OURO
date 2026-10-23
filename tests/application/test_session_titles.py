from __future__ import annotations

import pytest

from ouro.application.sessions.service import SessionService
from ouro.core.messages import MessageRecord
from ouro.core.session_commits import SessionCommit, SessionCommitStep
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


@pytest.fixture(params=["files", "sqlite"])
def services(tmp_path, request):
    store = (
        LocalSessionStore(tmp_path)
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    return store, SessionService(store)


def test_creation_distinguishes_absent_title_from_explicit_placeholder_text(services):
    store, sessions = services
    unnamed = sessions.create().session
    explicit = sessions.create(title="New conversation").session
    assert unnamed.title == explicit.title == "New conversation"
    assert unnamed.title_source == "placeholder"
    assert explicit.title_source == "fallback"
    assert store.load_session_record(unnamed.session_id).title_source == "placeholder"
    assert {entry.title_source for entry in sessions.catalog().entries} == {
        "placeholder",
        "fallback",
    }


async def test_generated_title_uses_bounded_context_without_loading_history(services, monkeypatch):
    store, sessions = services
    session = sessions.create().session
    for index in range(30):
        store.append_message(
            MessageRecord.text(session_id=session.session_id, role="user", text=str(index))
        )

    def reject(*_args, **_kwargs):
        pytest.fail("Title generation must not load complete history")

    monkeypatch.setattr(store, "load_session", reject)
    monkeypatch.setattr(store, "load_runtime_session", reject)

    async def generator(_session, messages):
        assert [item.text_content for item in messages] == [str(i) for i in range(20)]
        return "  Generated\n title  "

    result = await sessions.generate_title(session.session_id, generator)
    assert result.session.title == "Generated title"
    assert result.session.title_source == "generated"
    assert not result.session.title_locked


async def test_rename_at_generator_write_boundary_wins_without_another_write(services, monkeypatch):
    store, sessions = services
    session = sessions.create().session
    acquire = store.acquire_session_lock
    renamed = None

    def rename_before_lock(session_id, owner):
        nonlocal renamed
        monkeypatch.setattr(store, "acquire_session_lock", acquire)
        renamed = sessions.rename(session_id, "My title").session
        return acquire(session_id, owner)

    async def generator(_session, _messages):
        monkeypatch.setattr(store, "acquire_session_lock", rename_before_lock)
        return "Generated title"

    result = await sessions.generate_title(session.session_id, generator)
    assert result.session == renamed
    assert store.load_session_record(session.session_id).title == "My title"


async def test_generator_failure_reports_current_user_title(services):
    _, sessions = services
    session = sessions.create().session

    async def generator(_session, _messages):
        sessions.rename(session.session_id, "My title")
        raise RuntimeError("Generation failed")

    result = await sessions.generate_title(session.session_id, generator)
    assert result.session.title == "My title"
    assert result.session.title_source == "user"
    assert result.diagnostics[0].code == "session_title_generation_failed"


async def test_generation_does_not_apply_a_result_from_withdrawn_history(services):
    store, sessions = services
    session_id = sessions.create().session.session_id
    message = MessageRecord.text(session_id=session_id, role="user", text="Original topic")
    message.metadata["session_input_source"] = "user"
    store.commit_session(
        SessionCommit(
            before=store.load_session_record(session_id),
            steps=(SessionCommitStep(message=message),),
        )
    )

    async def generator(_session, _messages):
        sessions.undo(session_id, message_id=message.message_id)
        return "Stale generated title"

    result = await sessions.generate_title(session_id, generator)
    assert result.session.title == "Original topic"
    assert result.session.history_epoch == 1
    assert result.diagnostics[0].code == "session_title_update_deferred"


@pytest.mark.parametrize("generated", [" \n ", "x" * 161])
async def test_invalid_generated_title_preserves_placeholder(services, generated):
    store, sessions = services
    session = sessions.create().session

    async def generator(_session, _messages):
        return generated

    result = await sessions.generate_title(session.session_id, generator)
    assert result.session == session
    assert result.diagnostics[0].code == "session_title_generation_failed"
    assert store.load_session_record(session.session_id).title_source == "placeholder"


async def test_busy_session_defers_generated_title(services):
    store, sessions = services
    session = sessions.create().session
    lock = store.acquire_session_lock(session.session_id, "test")
    assert lock is not None

    async def generator(_session, _messages):
        return "Generated title"

    try:
        result = await sessions.generate_title(session.session_id, generator)
    finally:
        lock.release()
    assert result.session == session
    assert result.diagnostics[0].code == "session_title_update_deferred"
