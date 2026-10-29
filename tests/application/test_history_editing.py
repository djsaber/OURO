import asyncio

import pytest

from ouro.application.runtime.service import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import utc_now
from ouro.core.errors import SessionBusyError
from ouro.core.messages import MessageRecord
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.session_commits import SessionCommit
from ouro.core.state import RunContinuationCheckpoint, RunRecord, SessionRecord
from ouro.core.tool_batches import ToolBatchCall, ToolCallBatchRecord
from ouro.core.tools import ToolCallRequest, ToolResult
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from tests.event_capture import recorded_events
from tests.response_drafts import persist_response_draft
from tests.run_facts import run_events
from tests.tool_batches import bind_tool_messages, persist_tool_batch


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    result = (
        LocalSessionStore(tmp_path / "files")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "history.sqlite")
    )
    result.initialize()
    return result


def runtime_for(store, provider=None):
    return RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider or FauxProvider(), ModelMetadata(model_id="test", provider_id="faux")
        ),
        tools=ToolRegistry(),
    )


@pytest.mark.parametrize("new_stream", [False, True])
async def test_restart_after_continue_recovers_only_the_current_attempt(store, new_stream):
    runtime = runtime_for(store)
    session = runtime.create_session(title="Crash during continuation")
    run = RunRecord(session_id=session.session_id, status="running")
    store.save_run(run)
    user = MessageRecord.text(
        session_id=session.session_id, run_id=run.run_id, role="user", text="task", sequence=1
    )
    store.append_message(user)
    previous = persist_response_draft(store, run, turn_id="previous_turn", text="discarded output")
    store.commit_session(
        SessionCommit(
            before=store.load_session_record(session.session_id),
            runs=(
                store.load_run(session.session_id, run.run_id).model_copy(
                    update={"current_request_id": None}
                ),
            ),
            request_header=previous.model_copy(update={"response_closed_at": utc_now()}),
        )
    )
    if new_stream:
        persist_response_draft(store, run, turn_id="current_turn", text="current output")
    try:
        recovered = runtime.session_recovery.recover_runtime(session.session_id)
        assert recovered.runs[0].status == "recoverable"
        assert [
            message.text_content
            for message in store.load_run_messages(session.session_id, run.run_id)
        ] == (["task", "current output"] if new_stream else ["task"])
    finally:
        await runtime.close()


@pytest.mark.parametrize("status", ["aborted", "completed"])
async def test_continue_waits_for_stopped_execution_to_release_ownership(
    store, monkeypatch, status
):
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(session_id=session.session_id, status=status, ended_at=utc_now())
    store.save_run(run)
    store.append_message(
        MessageRecord.text(
            session_id=session.session_id, run_id=run.run_id, role="user", text="retained input"
        )
    )
    runtime = runtime_for(store)
    lock = await runtime.run_ownership.acquire(session.session_id, run.run_id)
    attempted = asyncio.Event()
    acquire = runtime.run_ownership.acquire

    async def observe_acquire(*args, **kwargs):
        result = await acquire(*args, **kwargs)
        attempted.set()
        return result

    monkeypatch.setattr(runtime.run_ownership, "acquire", observe_acquire)
    task = asyncio.create_task(runtime.continue_session(session_id=session.session_id))
    try:
        await asyncio.wait_for(attempted.wait(), 5)
        assert not task.done()
    finally:
        await runtime.run_ownership.release(run.run_id, lock)
        try:
            result = await asyncio.wait_for(task, 5)
            assert result.run.status == "completed"
            assert (result.run.run_id == run.run_id) == (status == "aborted")
        finally:
            await runtime.close()


@pytest.mark.parametrize("status", ["failed", "aborted", "recoverable", "running", "paused"])
async def test_continue_keeps_run_input_and_partial_history(store, status):
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(
        session_id=session.session_id,
        status=status,
        ended_at=None if status == "running" else utc_now(),
    )
    store.save_run(run)
    prompt = MessageRecord.text(
        session_id=session.session_id,
        run_id=run.run_id,
        role="user",
        text="original task",
        sequence=1,
    )
    partial = MessageRecord.text(
        session_id=session.session_id,
        run_id=run.run_id,
        role="assistant",
        text="unfinished",
        sequence=2,
        turn_id="turn_incomplete",
    ).model_copy(update={"metadata": {"incomplete": True}})
    store.append_messages([prompt, partial])
    store.save_session(
        session.model_copy(
            update={
                "message_count": 2,
                "last_message_id": partial.message_id,
                "last_run_id": run.run_id,
            }
        )
    )
    runtime = runtime_for(store)
    try:
        result = await runtime.continue_run(session_id=session.session_id)
        snapshot = store.load_session(session.session_id)
        assert result.run.status == "completed"
        assert result.run.run_id == run.run_id
        assert len(snapshot.runs) == 1
        assert snapshot.messages[0] == prompt
        assert len(snapshot.messages) == 3
        assert snapshot.messages[1] == partial
        assert snapshot.messages[2].text_content == "Hello from OURO."
        assert snapshot.session.message_count == 3
        assert snapshot.session.history_epoch == 0
        assert not any(
            event.event_type == "user_message_added" for event in run_events(store, result)
        )
        with pytest.raises(ValueError, match="completed"):
            await runtime.continue_run(session_id=session.session_id)
    finally:
        await runtime.close()


async def test_session_continuation_recovers_an_interrupted_execution(store):
    runtime = runtime_for(store)
    session = runtime.create_session()
    run = RunRecord(session_id=session.session_id, status="running")
    store.save_run(run)
    store.save_session(
        session.model_copy(update={"active_run_id": run.run_id, "status": "running"})
    )
    prompt = MessageRecord.text(
        session_id=session.session_id, run_id=run.run_id, role="user", text="retained work"
    )
    store.append_message(prompt)
    try:
        result = await runtime.continue_session(session_id=session.session_id)
        assert result.run.status == "completed"
        assert result.run.run_id == run.run_id
        assert store.load_session(session.session_id).messages[0] == prompt
    finally:
        await runtime.close()


async def test_undo_edits_original_session_without_loading_retained_messages(store, monkeypatch):
    runtime = runtime_for(store)
    try:
        first = await runtime.run("first question")
        second = await runtime.run("edit this question", session_id=first.session.session_id)
        before = store.load_session(first.session.session_id)

        def forbid_full_load(*args, **kwargs):
            raise AssertionError("undo must only read a suffix")

        with monkeypatch.context() as patch:
            patch.setattr(store, "load_runtime_session", forbid_full_load)
            patch.setattr(store, "load_session", forbid_full_load)
            patch.setattr(store, "load_runs", forbid_full_load)
            removed = runtime.sessions.undo(first.session.session_id)
        after = store.load_session(first.session.session_id)
        assert removed.text_content == "edit this question"
        assert removed.run_id == second.run.run_id
        assert after.messages == before.messages[:2]
        assert after.session.message_count == 2
        assert after.session.session_id == before.session.session_id
        revised = await runtime.run("revised question", session_id=first.session.session_id)
        assert revised.run.status == "completed"
        assert [
            m.text_content
            for m in store.load_session(first.session.session_id).messages
            if m.role == "user"
        ] == ["first question", "revised question"]
    finally:
        await runtime.close()


async def test_undo_incomplete_exchange_then_edit_and_continue_in_original_session(store):
    class PartialFailureProvider(FauxProvider):
        async def stream(self, **kwargs):
            yield ProviderStreamEvent(type="text_delta", text="partial output")
            raise RuntimeError("connection interrupted")

    runtime = runtime_for(store, PartialFailureProvider())
    try:
        first = await runtime.run("initial question")
        second = await runtime.run("continue", session_id=first.session.session_id)
        before = store.load_session(first.session.session_id)
        assert before.messages[1].metadata["incomplete"]
        selected = runtime.sessions.undo(first.session.session_id)
        assert selected.text_content == "continue"
        assert store.load_session(first.session.session_id).messages == before.messages[:2]
        continued = await runtime.continue_run(session_id=first.session.session_id)
        assert continued.run.run_id != first.run.run_id
        assert continued.run.history_epoch == 1
        revised = await runtime.run("revised input", session_id=first.session.session_id)
        assert revised.run.status == "failed"
        # The next incomplete frame may be cut even though an older attempt also failed.
        recovered = await runtime.continue_run(session_id=first.session.session_id)
        assert recovered.run.run_id == revised.run.run_id
        assert recovered.run.run_id != second.run.run_id
        assert store.load_session(first.session.session_id).messages[0] == before.messages[0]
    finally:
        await runtime.close()


async def test_continue_paused_agent_after_compaction_uses_original_run(store):
    class ObservedProvider(FauxProvider):
        async def stream(self, **kwargs):
            live = runtime.session_recovery.recover_runtime(session_id)
            assert live.session.active_run_id == original_run_id
            async for event in super().stream(**kwargs):
                yield event

    runtime = runtime_for(store)
    try:
        result = await runtime.run("original task")
        store.save_run(result.run.model_copy(update={"status": "paused"}))
        maintenance = RunRecord(
            session_id=result.session.session_id,
            kind="compaction",
            status="completed",
            ended_at=utc_now(),
        )
        store.save_run(maintenance)
        session_id, original_run_id = result.session.session_id, result.run.run_id
        await runtime.close()
        runtime = runtime_for(store, ObservedProvider())
        resumed = await runtime.continue_run(session_id=result.session.session_id)
        assert resumed.run.run_id == result.run.run_id
        assert resumed.run.status == "completed"
        assert len(store.load_runs(result.session.session_id)) == 2
        recovered = runtime.session_recovery.recover_runtime(result.session.session_id)
        assert recovered.session.last_run_id == result.run.run_id
        with pytest.raises(ValueError, match="completed"):
            await runtime.continue_run(session_id=result.session.session_id)
    finally:
        await runtime.close()


@pytest.mark.parametrize("completed", [False, True])
async def test_continue_settles_interrupted_terminal_checkpoint_before_reopening(store, completed):
    runtime = runtime_for(
        store,
        FauxProvider(
            event_batches=[
                [
                    ProviderStreamEvent(type="text_delta", text="first"),
                    ProviderStreamEvent(type="done"),
                ]
                if completed
                else [],
                [
                    ProviderStreamEvent(type="text_delta", text="done"),
                    ProviderStreamEvent(type="done"),
                ],
            ]
        ),
    )
    try:
        failed = await runtime.run("original task")
        checkpoint = RunContinuationCheckpoint()
        store.save_run(failed.run.model_copy(update={"continuation": checkpoint}))
        resumed = (
            await runtime.continue_session(session_id=failed.session.session_id)
            if completed
            else await runtime.continue_run(session_id=failed.session.session_id)
        )
        assert (resumed.run.run_id == failed.run.run_id) is not completed
        assert resumed.run.status == "completed"
        if completed:
            assert (
                store.load_run(failed.session.session_id, failed.run.run_id).continuation.state
                == "resolved"
            )
            return
        events = run_events(store, resumed)
        resolved = next(
            event for event in events if event.event_type == "run_continuation_resolved"
        )
        reopened = next(event for event in events if event.payload.get("resumed"))
        assert resolved.payload["decision_id"] == checkpoint.decision_id
        assert resolved.sequence < reopened.sequence
    finally:
        await runtime.close()


async def test_undo_empty_and_live_session_are_rejected_without_changes(store):
    runtime = runtime_for(store)
    session = runtime.create_session(title="Test")
    try:
        with pytest.raises(ValueError, match="no user message"):
            runtime.sessions.undo(session.session_id)
        result = await runtime.run("question", session_id=session.session_id)
        owner = store.acquire_session_lock(session.session_id, result.run.run_id)
        assert owner is not None
        try:
            with pytest.raises(SessionBusyError, match="Abort the active run"):
                runtime.sessions.undo(session.session_id)
        finally:
            owner.release()
        assert len(store.load_session(session.session_id).messages) == 2
    finally:
        await runtime.close()


async def test_continue_can_retry_repeated_provider_failure_without_extra_runs(store):
    provider = FauxProvider(
        event_batches=[
            [],
            [],
            [ProviderStreamEvent(type="text_delta", text="done"), ProviderStreamEvent(type="done")],
        ]
    )
    runtime = runtime_for(store, provider)
    try:
        first = await runtime.run("question")
        assert first.run.status == "failed"
        second = await runtime.continue_run(session_id=first.session.session_id)
        assert second.run.status == "failed"
        third = await runtime.continue_run(session_id=first.session.session_id)
        assert third.run.status == "completed"
        assert third.run.run_id == second.run.run_id == first.run.run_id
        assert len(store.load_runs(first.session.session_id)) == 1
        assert [
            event.payload["round"]
            for event in recorded_events(first.session.session_id)
            if event.event_type == "model_request_started"
        ] == [1, 2, 3]
    finally:
        await runtime.close()


async def test_continue_keeps_checkpointed_tool_result_and_closes_unknown_outcome(store):
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(session_id=session.session_id, status="failed", ended_at=utc_now())
    user = MessageRecord.text(
        session_id=session.session_id, run_id=run.run_id, role="user", text="inspect", sequence=1
    )
    calls = [ToolCallRequest(id=f"call_{index}", name="Read", arguments={}) for index in range(2)]
    assistant = MessageRecord.text(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id="turn_tools",
        role="assistant",
        text="",
        sequence=2,
    ).model_copy(update={"tool_calls": calls})
    checkpoint = ToolResult(tool_call_id=calls[0].id, tool_name="Read", content="saved evidence")
    batch = ToolCallBatchRecord(
        assistant_message_id=assistant.message_id,
        turn_id="turn_tools",
        phase="recoverable",
        calls=[
            ToolBatchCall(request=calls[0], status="completed", result=checkpoint),
            ToolBatchCall(request=calls[1], status="unknown_outcome"),
        ],
    )
    run = persist_tool_batch(store, run, batch)
    bind_tool_messages(store, run, batch, [assistant])
    store.append_messages([user, assistant])
    store.save_session(
        session.model_copy(
            update={
                "message_count": 2,
                "last_message_id": assistant.message_id,
                "last_run_id": run.run_id,
            }
        )
    )
    runtime = runtime_for(store)
    try:
        continued = await runtime.continue_run(session_id=session.session_id)
        messages = store.load_session(session.session_id).messages
        assert continued.run.run_id == run.run_id
        assert continued.run.status == "completed"
        assert messages[:2] == [user, assistant]
        assert messages[2].text_content == "saved evidence"
        assert messages[2].tool_result.is_error is False
        assert messages[3].tool_result.is_error is True
        assert "unknown" in messages[3].text_content
        assert [m.role for m in messages] == [
            "user",
            "assistant",
            "tool_result",
            "tool_result",
            "assistant",
        ]
    finally:
        await runtime.close()


async def test_continue_does_not_cut_incomplete_output_from_an_older_run(store):
    runtime = runtime_for(store)
    try:
        old = await runtime.run("old question")
        tail = store.load_messages_tail(old.session.session_id, limit=1)[0]
        runtime.sessions.undo(old.session.session_id)
        old_input = MessageRecord.text(
            session_id=old.session.session_id,
            run_id=old.run.run_id,
            role="user",
            text="historical input",
            sequence=1,
        )
        old_partial = tail.model_copy(update={"metadata": {"incomplete": True}, "sequence": 2})
        store.append_messages([old_input, old_partial])
        current = await runtime.run("current question", session_id=old.session.session_id)
        # A crash after committing the current input/output must not turn an
        # older retained recovery frame into the new truncation boundary.
        store.save_run(current.run.model_copy(update={"status": "failed"}))
        before = store.load_session(old.session.session_id).messages
        result = await runtime.continue_run(session_id=old.session.session_id)
        after = store.load_session(old.session.session_id).messages
        assert result.run.run_id == current.run.run_id
        assert after[: len(before)] == before
    finally:
        await runtime.close()


async def test_failure_keeps_tool_checkpoint_for_same_run_continuation(store):
    runtime = runtime_for(store)
    session = runtime.create_session(title="Tool checkpoint")
    request = ToolCallRequest(id="saved_call", name="Read", arguments={})
    saved_result = ToolResult(
        tool_call_id=request.id, tool_name=request.name, content="saved output"
    )
    run = RunRecord(session_id=session.session_id, status="running")
    user = MessageRecord.text(
        session_id=session.session_id,
        run_id=run.run_id,
        role="user",
        text="read evidence",
        sequence=1,
    )
    assistant = MessageRecord.text(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id="saved_turn",
        role="assistant",
        text="",
        sequence=2,
    ).model_copy(update={"tool_calls": [request]})
    batch = ToolCallBatchRecord(
        assistant_message_id=assistant.message_id,
        turn_id="saved_turn",
        phase="committing_results",
        calls=[ToolBatchCall(request=request, status="completed", result=saved_result)],
    )
    run = persist_tool_batch(store, run, batch)
    bind_tool_messages(store, run, batch, [assistant])
    store.append_messages([user, assistant])
    store.save_session(
        session.model_copy(
            update={
                "active_run_id": run.run_id,
                "last_run_id": run.run_id,
                "message_count": 2,
                "last_message_id": assistant.message_id,
            }
        )
    )
    try:
        failed = await runtime.run_lifecycle.fail(
            session=session,
            run=run,
            error="result commit interrupted",
            final_text="",
            had_tool_error=False,
        )
        assert failed.run.active_tool_batch == batch.head()
        result = await runtime.continue_run(session_id=session.session_id)
        assert result.run.status == "completed"
        messages = store.load_session(session.session_id).messages
        assert messages[2].text_content == "saved output"
        assert messages[2].tool_result.is_error is False
    finally:
        await runtime.close()


async def test_concurrent_continue_cannot_take_over_live_execution(store):
    entered, release = asyncio.Event(), asyncio.Event()

    class GatedRetryProvider(FauxProvider):
        attempts = 0

        async def stream(self, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                yield ProviderStreamEvent(type="done")
                return
            entered.set()
            await release.wait()
            async for event in super().stream(**kwargs):
                yield event

    provider = GatedRetryProvider()
    runtime = runtime_for(store, provider)
    other = runtime_for(store, provider)
    continuing = None
    try:
        failed = await runtime.run("original input")
        continuing = asyncio.create_task(runtime.continue_run(session_id=failed.session.session_id))
        async with asyncio.timeout(5):
            await entered.wait()
        with pytest.raises((ValueError, SessionBusyError)):
            await other.continue_run(session_id=failed.session.session_id)
        release.set()
        result = await continuing
        assert result.run.run_id == failed.run.run_id
        assert result.run.status == "completed"
        assert provider.attempts == 2
        assert len(store.load_runs(failed.session.session_id)) == 1
    finally:
        release.set()
        if continuing is not None:
            await continuing
        await other.close()
        await runtime.close()
