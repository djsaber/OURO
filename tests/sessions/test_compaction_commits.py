from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import utc_now
from ouro.core.compaction import CompactionRecord
from ouro.core.context import ContextMetrics
from ouro.core.context_snapshots import ModelRequestHeader
from ouro.core.errors import PersistenceError
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata
from ouro.core.session_commits import SessionCommit, SessionCommitConflict
from ouro.core.state import RunRecord, SessionRecord
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from tests.session_storage import session_connection
from tests.session_summaries import commit_summary


@pytest.fixture(params=[LocalSessionStore, SQLiteSessionStore])
def store(tmp_path, request):
    result = request.param(
        tmp_path / "sessions.sqlite3" if request.param is SQLiteSessionStore else tmp_path / "files"
    )
    result.initialize()
    return result


def history(store):
    session = SessionRecord()
    store.create_session(session)
    store.append_messages(
        [
            MessageRecord.text(
                session_id=session.session_id,
                role="user" if index % 2 == 0 else "assistant",
                text=f"message {index}",
            )
            for index in range(4)
        ]
    )
    messages = list(reversed(store.load_messages_tail(session.session_id)))
    return store.load_session_record(session.session_id), messages


def summary(session, messages, *, count=2):
    return CompactionRecord(
        session_id=session.session_id,
        previous_compaction_id=session.context_selection.compaction_id,
        strategy_id="test",
        trigger="manual",
        summary="summary of the covered prefix",
        covered_message_count=count,
        covered_end_message_id=messages[count - 1].message_id,
        history_boundary_message_id=messages[-1].message_id,
    )


def test_snapshot_materializes_canonical_positions_for_later_summary(store):
    session = SessionRecord()
    messages = [
        MessageRecord.text(session_id=session.session_id, role="user", text=f"message {index}")
        for index in range(4)
    ]
    assert all(item.sequence == 0 for item in messages)
    store.create_session_snapshot(session, messages)
    candidate = summary(session, messages)
    commit_summary(store, candidate)
    assert store.load_active_compaction(session.session_id) == candidate
    stored = list(reversed(store.load_messages_tail(session.session_id)))
    assert [item.sequence for item in stored] == [1, 2, 3, 4]
    assert [item.message_id for item in stored] == [item.message_id for item in messages]


@pytest.mark.parametrize(
    "invalid", ["position", "missing", "skill", "foreign", "predecessor", "duplicate", "active"]
)
def test_snapshot_rejects_invalid_summary_dependencies_before_publication(store, invalid):
    session = SessionRecord()
    messages = [
        MessageRecord.text(session_id=session.session_id, role="user", text=f"message {index}")
        for index in range(4)
    ]
    candidate = summary(session, messages)
    updates = {
        "position": {"covered_message_count": 1},
        "missing": {"covered_end_message_id": "msg_missing"},
        "skill": {"retained_skill_message_ids": (messages[-1].message_id,)},
        "foreign": {"session_id": "ses_foreign"},
        "predecessor": {"previous_compaction_id": "cmp_missing"},
    }.get(invalid, {})
    records = [candidate.model_copy(update=updates)]
    if invalid == "duplicate":
        records.append(candidate)
    if invalid == "active":
        session.context_selection.compaction_id = "cmp_missing"
    with pytest.raises(PersistenceError):
        store.create_session_snapshot(session, messages, compactions=records)
    with pytest.raises(FileNotFoundError):
        store.load_session_record(session.session_id)


@pytest.mark.parametrize("change", ["append", "unrelated", "withdraw", "competing_summary"])
async def test_activation_checks_only_its_captured_dependencies(store, change):
    session, messages = history(store)
    candidate = summary(session, messages)
    run = RunRecord(session_id=session.session_id, history_epoch=session.history_epoch)
    store.save_run(run)
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="test", provider_id="test")),
        tools=ToolRegistry(),
    )
    generated, release = asyncio.Event(), asyncio.Event()

    async def finish_candidate():
        generated.set()
        await release.wait()
        await asyncio.to_thread(
            runtime.context_preparation._commit_compaction,
            candidate,
            run,
            selection=session.context_selection.model_copy(
                update={"compaction_id": candidate.compaction_id}
            ),
            expected_selection=session.context_selection,
        )

    task = asyncio.create_task(finish_candidate())
    try:
        await asyncio.wait_for(generated.wait(), 5)
        if change == "append":
            store.append_message(
                MessageRecord.text(session_id=session.session_id, role="user", text="new tail")
            )
        elif change == "unrelated":
            store.save_session(session.model_copy(update={"title": "new title"}))
        elif change == "withdraw":
            store.truncate_messages(
                session.session_id,
                before_message_id=messages[-1].message_id,
                expected_last_message_id=messages[-1].message_id,
            )
        else:
            commit_summary(store, summary(session, messages))
        before = store.load_session_record(session.session_id)
        release.set()
        if change in {"withdraw", "competing_summary"}:
            with pytest.raises(SessionCommitConflict):
                await task
            assert store.load_session_record(session.session_id) == before
        else:
            await task
            saved = store.load_session_record(session.session_id)
            assert saved.context_selection.compaction_id == candidate.compaction_id
            assert saved.last_message_id == before.last_message_id
            assert saved.title == before.title
            assert store.load_active_compaction(session.session_id) == candidate
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.parametrize("invalid", ["position", "missing", "skill_outside_coverage"])
def test_activation_rejects_invalid_fixed_references(store, invalid):
    session, messages = history(store)
    candidate = summary(session, messages)
    updates = {
        "position": {"covered_message_count": 1},
        "missing": {"covered_end_message_id": "msg_missing"},
        "skill_outside_coverage": {"retained_skill_message_ids": (messages[-1].message_id,)},
    }[invalid]
    with pytest.raises(SessionCommitConflict):
        commit_summary(store, candidate.model_copy(update=updates))
    assert store.load_session_record(session.session_id) == session
    assert store.load_compactions(session.session_id) == []


def test_tail_withdrawal_preserves_summary_and_moves_only_its_display_anchor(store):
    session, messages = history(store)
    candidate = summary(session, messages)
    commit_summary(store, candidate)
    store.truncate_messages(
        session.session_id,
        before_message_id=messages[2].message_id,
        expected_last_message_id=messages[-1].message_id,
    )
    active = store.load_active_compaction(session.session_id)
    assert active is not None and active.compaction_id == candidate.compaction_id
    assert active.summary == candidate.summary
    assert active.covered_end_message_id == candidate.covered_end_message_id
    assert active.history_boundary_message_id == messages[1].message_id
    assert store.load_session_record(session.session_id).history_epoch == session.history_epoch + 1
    store.truncate_messages(
        session.session_id,
        before_message_id=messages[1].message_id,
        expected_last_message_id=messages[1].message_id,
    )
    assert store.load_active_compaction(session.session_id) is None
    assert store.load_compactions(session.session_id) == []


async def test_native_failure_never_exposes_a_half_activated_summary(tmp_path, store, monkeypatch):
    session, messages = history(store)
    candidate = summary(session, messages)
    run = RunRecord(session_id=session.session_id, history_epoch=session.history_epoch)
    run.current_request_id = "req_summary"
    header = ModelRequestHeader(
        session_id=session.session_id,
        run_id=run.run_id,
        context_id=run.current_request_id,
        turn_id="req_summary",
        purpose="compaction",
        model_round=1,
        provider_id="test",
        model_id="test",
        started_at=utc_now(),
        provider_done_at=utc_now(),
        metrics=ContextMetrics(estimated_input_tokens=1, estimated_fixed_tokens=0),
    )
    store.commit_session(SessionCommit(before=session, runs=(run,), request_header=header))
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="test", provider_id="test")),
        tools=ToolRegistry(),
    )

    def activate():
        return runtime.context_preparation._commit_compaction(
            candidate,
            run,
            request_id=header.context_id,
            selection=session.context_selection.model_copy(
                update={"compaction_id": candidate.compaction_id}
            ),
            expected_selection=session.context_selection,
        )

    with session_connection(store, session.session_id) as connection:
        connection.execute(
            "CREATE TRIGGER fail_activation BEFORE INSERT ON events "
            "BEGIN SELECT RAISE(ABORT, 'activation failed'); END"
        )
    with pytest.raises((PersistenceError, sqlite3.IntegrityError), match="activation failed"):
        activate()
    cold = (
        LocalSessionStore(tmp_path / "files")
        if isinstance(store, LocalSessionStore)
        else SQLiteSessionStore(store.database)
    )
    cold.initialize()
    assert cold.load_request_header(session.session_id, header.context_id) == header
    assert cold.load_active_compaction(session.session_id) is None
    assert cold.load_compactions(session.session_id) == []
    assert cold.load_events_page(session.session_id, limit=100)[0] == []
    with session_connection(store, session.session_id) as connection:
        connection.execute("DROP TRIGGER fail_activation")
    activate()
    settled = cold.load_request_header(session.session_id, header.context_id)
    assert settled.response_compaction_id == candidate.compaction_id
    assert settled.response_closed_at is not None
    assert cold.load_active_compaction(session.session_id) == candidate
    assert cold.load_compactions(session.session_id) == [candidate]
    assert cold.load_session_record(session.session_id).last_message_id == messages[-1].message_id
    with cold.session_mutation(session.session_id):
        pass
    assert cold.load_compactions(session.session_id) == [candidate]

    events, _ = cold.load_events_page(session.session_id, limit=100)
    assert len(events) == 1
    assert events[0].event_type == "compaction_completed"
    assert events[0].payload["compaction_id"] == candidate.compaction_id
    assert events[0].run_id == run.run_id
    await runtime.close()


def test_active_summary_is_a_point_read_without_history_or_old_summaries(store, monkeypatch):
    session, messages = history(store)
    old = summary(session, messages)
    commit_summary(store, old)
    current = summary(store.load_session_record(session.session_id), messages)
    commit_summary(store, current)
    database = (
        store.sessions_dir / session.session_id / "session-data.sqlite3"
        if isinstance(store, LocalSessionStore)
        else store.database
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE compactions SET payload = '{broken' WHERE compaction_id = ?",
            (old.compaction_id,),
        )
    open_file = Path.open

    def bounded_open(path, *args, **kwargs):
        assert path.name != "messages.jsonl"
        return open_file(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", bounded_open)
        assert store.load_active_compaction(session.session_id) == current
    with pytest.raises((PersistenceError, ValueError)):
        store.load_compactions(session.session_id)
