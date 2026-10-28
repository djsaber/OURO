import asyncio
import sqlite3
from contextlib import closing

import pytest

import ouro.sessions.backends.files.commits as file_commits
from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.cancellation import AbortSignal
from ouro.core.compaction import CompactionRecord
from ouro.core.context import ContextBuilder
from ouro.core.context_selection import MessageBoundary
from ouro.core.context_snapshots import ContextSnapshotRecord
from ouro.core.errors import PersistenceError, SessionRecoveryRequiredError
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata
from ouro.core.session_commits import SessionCommitConflict
from ouro.core.state import RunRecord, SessionRecord
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from tests.context_facts import save_context
from tests.session_summaries import commit_summary


@pytest.fixture(params=[LocalSessionStore, SQLiteSessionStore])
def store(tmp_path, request):
    result = request.param(
        tmp_path / "sessions.db" if request.param is SQLiteSessionStore else tmp_path
    )
    result.initialize()
    return result


def history(store):
    session = SessionRecord()
    store.create_session(session)
    messages = [
        MessageRecord.text(session_id=session.session_id, role="user", text=str(i))
        for i in range(4)
    ]
    store.append_messages(messages)
    return store.load_session_record(session.session_id), messages


def select_through(store, session, message):
    selection = session.context_selection.model_copy(
        update={
            "media_elided_through": MessageBoundary(
                message_id=message.message_id, sequence=message.sequence
            ),
        }
    )
    store.save_session(session.model_copy(update={"context_selection": selection}))
    return store.load_session_record(session.session_id)


@pytest.mark.parametrize("invalid", ["missing", "position", "foreign"])
def test_selection_rejects_invalid_media_boundary_without_state_change(store, invalid):
    session, messages = history(store)
    source = messages[1]
    if invalid == "foreign":
        _, foreign = history(store)
        source = foreign[1]
    boundary = MessageBoundary(
        message_id="msg_missing" if invalid == "missing" else source.message_id,
        sequence=1 if invalid == "position" else source.sequence,
    )
    with pytest.raises(SessionCommitConflict, match="media selection"):
        store.save_session(
            session.model_copy(
                update={
                    "context_selection": session.context_selection.model_copy(
                        update={"media_elided_through": boundary}
                    ),
                }
            )
        )
    assert store.load_session_record(session.session_id) == session


def test_selection_survives_request_replacement_and_can_restore_media(store):
    session, messages = history(store)
    selected = select_through(store, session, messages[1])
    view = ContextBuilder().build(messages=messages, tools=[])
    for turn in ("one", "two"):
        context = ContextSnapshotRecord(
            session_id=session.session_id,
            run_id="run_test",
            turn_id=turn,
            model_round=1,
            provider_id="faux",
            model_id="faux",
            metrics=view.metrics,
            provider_context=view.provider_context,
            context_metadata={"audit": turn},
        )
        save_context(store, context)
        assert (
            store.load_session_record(session.session_id).context_selection
            == selected.context_selection
        )
    store.save_session(
        selected.model_copy(
            update={
                "context_selection": selected.context_selection.model_copy(
                    update={"media_elided_through": None}
                ),
            }
        )
    )
    assert (
        store.load_session_record(session.session_id).context_selection.media_elided_through is None
    )
    assert store.load_latest_context(session.session_id) == context


def test_withdrawal_clamps_selection_without_reopening_older_media(store):
    session, messages = history(store)
    selected = select_through(store, session, messages[2])
    store.truncate_messages(
        session.session_id,
        before_message_id=messages[3].message_id,
        expected_last_message_id=messages[3].message_id,
    )
    assert (
        store.load_session_record(session.session_id).context_selection
        == selected.context_selection
    )
    store.truncate_messages(
        session.session_id,
        before_message_id=messages[2].message_id,
        expected_last_message_id=messages[2].message_id,
    )
    selection = store.load_session_record(session.session_id).context_selection
    assert selection.media_elided_through == MessageBoundary(
        message_id=messages[1].message_id, sequence=2
    )
    assert selection.elided_ids(messages[:2]) == frozenset(item.message_id for item in messages[:2])
    store.truncate_messages(
        session.session_id,
        before_message_id=messages[0].message_id,
        expected_last_message_id=messages[1].message_id,
    )
    assert (
        store.load_session_record(session.session_id).context_selection.media_elided_through is None
    )


def test_snapshot_rejects_dangling_selection_before_publication(store):
    session = SessionRecord()
    session.context_selection.media_elided_through = MessageBoundary(
        message_id="msg_missing", sequence=1
    )
    with pytest.raises(PersistenceError, match="media selection"):
        store.create_session_snapshot(session, [])
    with pytest.raises(FileNotFoundError):
        store.load_session_record(session.session_id)


@pytest.mark.parametrize("cut_index", [1, 3])
def test_withdrawal_preserves_explicit_full_history_selection(store, cut_index):
    session, messages = history(store)
    summary = CompactionRecord(
        session_id=session.session_id,
        strategy_id="test",
        trigger="manual",
        summary="Retained summary is available, but not selected",
        covered_message_count=2,
        covered_end_message_id=messages[1].message_id,
        history_boundary_message_id=messages[2].message_id,
    )
    commit_summary(store, summary)
    selected = store.load_session_record(session.session_id)
    store.save_session(selected.model_copy(update={"context_selection": session.context_selection}))
    store.truncate_messages(
        session.session_id,
        before_message_id=messages[cut_index].message_id,
        expected_last_message_id=messages[-1].message_id,
    )
    assert store.load_session_record(session.session_id).context_selection.compaction_id is None
    assert bool(store.load_compactions(session.session_id)) == (cut_index == 3)


def test_selection_rejects_missing_summary_without_overwriting_current_state(store):
    session, _ = history(store)
    with pytest.raises(SessionCommitConflict, match="selected compaction"):
        store.save_session(
            session.model_copy(
                update={
                    "context_selection": session.context_selection.model_copy(
                        update={"compaction_id": "cmp_missing"}
                    ),
                }
            )
        )
    assert store.load_session_record(session.session_id) == session


@pytest.mark.parametrize("change", ["selection", "title"])
async def test_preparation_checks_selection_dependency_but_accepts_unrelated_change(
    store, monkeypatch, change
):
    session, messages = history(store)
    run = RunRecord(session_id=session.session_id)
    runtime = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id="test", provider_id="test"),
        ),
    )
    session, run, _ = runtime.run_lifecycle.start(
        session=session, run=run, snapshot=store.load_runtime_session(session.session_id)
    )
    captured, release = asyncio.Event(), asyncio.Event()
    resolve = runtime.context_preparation._resolve_prompt_context

    async def paused(*args, **kwargs):
        captured.set()
        await release.wait()
        return await resolve(*args, **kwargs)

    monkeypatch.setattr(runtime.context_preparation, "_resolve_prompt_context", paused)
    task = asyncio.create_task(
        runtime.context_preparation.prepare(
            0,
            session=session,
            run=run,
            tools=[],
            model_round=1,
            turn_id="turn_test",
            abort_signal=AbortSignal(),
        )
    )
    try:
        await asyncio.wait_for(captured.wait(), 5)
        if change == "selection":
            expected = select_through(store, session, messages[1])
        else:
            store.save_session(session.model_copy(update={"title": "changed title"}))
            expected = store.load_session_record(session.session_id)
        release.set()
        if change == "selection":
            with pytest.raises(SessionCommitConflict, match="selection changed"):
                await task
            assert store.load_latest_context(session.session_id) is None
        else:
            await task
            assert store.load_latest_context(session.session_id) is not None
        current = store.load_session_record(session.session_id)
        assert current.context_selection == expected.context_selection
        assert current.title == expected.title
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


def test_selection_native_failure_preserves_backend_commit_semantics(store, monkeypatch):
    session, messages = history(store)
    if isinstance(store, LocalSessionStore):

        def fail_session(connection, session):
            raise OSError("selection projection failure")

        with monkeypatch.context() as patch:
            patch.setattr(file_commits, "write_session", fail_session)
            with pytest.raises(PersistenceError, match="selection projection failure"):
                select_through(store, session, messages[1])
        cold = LocalSessionStore(store.root)
        cold.initialize()
        with pytest.raises(SessionRecoveryRequiredError):
            cold.load_session_record(session.session_id)
        with cold.session_mutation(session.session_id):
            pass
    else:
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_selection AFTER UPDATE ON sessions "
                "BEGIN SELECT RAISE(ABORT, 'selection failed'); END"
            )
        with pytest.raises((PersistenceError, sqlite3.IntegrityError), match="selection failed"):
            select_through(store, session, messages[1])
        cold = SQLiteSessionStore(store.database)
        cold.initialize()
        assert (
            cold.load_session_record(session.session_id).context_selection
            == session.context_selection
        )
        with closing(sqlite3.connect(store.database)) as connection, connection:
            connection.execute("DROP TRIGGER fail_selection")
        select_through(cold, session, messages[1])
    selection = cold.load_session_record(session.session_id).context_selection
    assert selection.media_elided_through == MessageBoundary(
        message_id=messages[1].message_id, sequence=2
    )
    assert cold.load_latest_context(session.session_id) is None
