from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from ouro.application.harness import capture_harness
from ouro.application.runtime.service import RuntimeService, _QueuedInputSelectionChanged
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import utc_now
from ouro.core.harness import HarnessState
from ouro.core.messages import (
    MessageRecord,
    ProviderContinuationArtifact,
    TextContent,
    ToolResultReference,
    model_visible_message,
)
from ouro.core.provider import ModelMetadata
from ouro.core.session_commits import SessionCommit, SessionCommitConflict, SessionCommitStep
from ouro.core.session_controls import (
    SessionControlActivation,
    SessionControlState,
    SessionInputRequest,
)
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import RunContinuationCheckpoint, RunRecord, SessionRecord
from ouro.core.tool_batches import ToolBatchCall, ToolCallBatchRecord
from ouro.core.tools import ToolCallRequest, ToolResult
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry
from tests.event_capture import recorded_events
from tests.tool_batches import bind_tool_messages, persist_tool_batch


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    store = (
        LocalSessionStore(tmp_path / "sessions")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    return store


def runtime_for(store):
    return RuntimeService(
        store=store,
        adapter=RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="test", provider_id="faux")),
        tools=ToolRegistry(),
    )


async def test_undo_restores_missing_plugin_data_but_execution_requires_its_contract(store):
    runtime = runtime_for(store)
    session = SessionRecord()
    store.create_session(session)
    owner = "example.unavailable"
    state = HarnessState(
        states=(
            SessionExtensionStateRecord(
                session_id=session.session_id,
                owner_id=owner,
                state_schema_version=99,
                payload={"opaque": ["preserve", 3]},
            ),
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
            ]
        ),
    )
    message = MessageRecord.text(session_id=session.session_id, role="user", text="withdraw")
    message.metadata["session_input_source"] = "user"
    store.commit_session(
        SessionCommit(
            before=session,
            steps=(
                SessionCommitStep(harness=state),
                SessionCommitStep(message=message, harness=HarnessState()),
            ),
        )
    )
    try:
        await runtime.undo(session.session_id)
        assert store.load_harness(session.session_id).state == state
        result = await runtime.run("retry", session_id=session.session_id)
        assert result.run.status == "failed"
        assert "unavailable" in result.run.error
        assert store.load_harness(session.session_id).state == state
    finally:
        await runtime.close()


@pytest.mark.parametrize("resume", [False, True])
async def test_withdrawn_active_run_is_audit_only_when_starting_current_history(store, resume):
    runtime = runtime_for(store)
    try:
        first = await runtime.run("retained input")
        old = first.run.model_copy(update={"status": "waiting_user", "ended_at": None})
        store.save_run(old)
        store.commit_session(
            SessionCommit(
                before=store.load_session_record(first.session.session_id),
                before_message_id=first.session.last_message_id,
            )
        )
        result = (
            await runtime.continue_run(session_id=first.session.session_id)
            if resume
            else await runtime.run("new input", session_id=first.session.session_id)
        )
        assert result.run.status == "completed"
        assert result.run.history_epoch == 1
        assert store.load_run(first.session.session_id, old.run_id) == old
    finally:
        await runtime.close()


@pytest.mark.parametrize("cut", [1, 2, 3])
async def test_exact_cut_then_continue_preserves_prefix_and_ignores_old_checkpoint(store, cut):
    runtime = runtime_for(store)
    session = runtime.create_session(title="Exact tool exchange cut")
    run = RunRecord(session_id=session.session_id, status="aborted", ended_at=utc_now())
    user = MessageRecord.text(
        session_id=session.session_id, run_id=run.run_id, role="user", text="inspect"
    )
    requests = [ToolCallRequest(id=f"call_{i}", name="Read", arguments={}) for i in range(2)]
    assistant = MessageRecord(
        session_id=session.session_id,
        run_id=run.run_id,
        role="assistant",
        turn_id="tools",
        tool_calls=requests,
    )
    results = [
        ToolResult(tool_call_id=item.id, tool_name=item.name, content=f"withdrawn {item.id}")
        for item in requests
    ]
    batch = ToolCallBatchRecord(
        assistant_message_id=assistant.message_id,
        turn_id="tools",
        phase="recoverable",
        calls=[
            ToolBatchCall(request=item, status="completed", result=result)
            for item, result in zip(requests, results, strict=True)
        ],
    )
    persist_tool_batch(
        store, run.model_copy(update={"continuation": RunContinuationCheckpoint()}), batch
    )
    messages = [
        user,
        assistant,
        *[
            MessageRecord(
                session_id=session.session_id,
                run_id=run.run_id,
                role="tool_result",
                turn_id="tools",
                content=[TextContent(text=result.content)],
                tool_result=ToolResultReference(
                    tool_call_id=result.tool_call_id, tool_name=result.tool_name
                ),
            )
            for result in results
        ],
    ]
    bind_tool_messages(store, run, batch, messages)
    committed = store.commit_session(
        SessionCommit(
            before=store.load_session_record(session.session_id),
            steps=tuple(
                SessionCommitStep(
                    message=message,
                    harness=HarnessState(
                        states=(
                            SessionExtensionStateRecord(
                                session_id=session.session_id,
                                owner_id="test.state",
                                state_schema_version=1,
                                payload=index,
                            ),
                        )
                    ),
                )
                for index, message in enumerate(messages)
            ),
        )
    )
    removed = store.truncate_messages(
        session.session_id,
        before_message_id=committed.messages[cut].message_id,
        expected_last_message_id=committed.messages[-1].message_id,
    )
    assert removed.first_removed == committed.messages[cut]
    assert store.load_harness(session.session_id).state.states[0].payload == cut - 1
    assert store.load_session_record(session.session_id).active_run_id is None
    assert store.load_session_record(session.session_id).last_run_id is None
    withdrawn_run = store.load_run(session.session_id, run.run_id)
    assert withdrawn_run.continuation.state == "resolved"
    assert withdrawn_run.continuation.reason == "session_history_withdrawn"
    assert store.load_pending_runs(session.session_id) == []
    try:
        await runtime.reconcile_execution_session(session.session_id)
        old_run = store.load_run(session.session_id, run.run_id)
        assert old_run == withdrawn_run
        assert old_run.active_tool_batch is not None
        assert old_run.continuation.reason == "session_history_withdrawn"
        result = await runtime.continue_run(session_id=session.session_id)
        assert result.run.run_id != run.run_id
        assert result.run.history_epoch == 1
        assert result.run.status == "completed"
        after = store.load_session(session.session_id)
        assert [message.as_fact() for message in after.messages[:-1]] == list(
            committed.messages[:cut]
        )
        assert after.messages[-1].text_content == "Hello from OURO."
        context = store.load_latest_context(session.session_id)
        assert context.provider_context.messages == [model_visible_message(user)]
        assert bool(context.context_metadata["history_diagnostics"]) == (cut > 1)
        assert store.load_run(session.session_id, run.run_id) == old_run
        with pytest.raises(SessionCommitConflict, match="withdrawn"):
            capture_harness(store, session.session_id, run=old_run)
    finally:
        await runtime.close()


def queued_batch(runtime, session):
    _, inputs, _, _, _ = runtime.queued_inputs.enqueue_batch(
        snapshot=runtime.store.load_runtime_session(session.session_id),
        inputs=(SessionInputRequest(prompt="first"), SessionInputRequest(prompt="second")),
        source="plugin",
        plugin_id="test.plugin",
        contribution_id="command",
        generation_id="generation",
        execution_contract_hash="contract",
    )
    return inputs


@pytest.mark.parametrize("boundary", ["start", "steer"])
@pytest.mark.parametrize("decided", [False, True])
async def test_queue_consumption_failure_is_one_native_decision(
    store, monkeypatch, boundary, decided
):
    runtime = runtime_for(store)
    session = runtime.create_session(title="Atomic consumption")
    inputs = queued_batch(runtime, session)
    run = RunRecord(session_id=session.session_id)
    if boundary == "steer":
        session, run, _ = runtime.run_lifecycle.start(
            session=session, run=run, snapshot=store.load_runtime_session(session.session_id)
        )
    snapshot = store.load_runtime_session(session.session_id)

    def interrupt(request):
        raise OSError("interrupted at decision")

    try:
        with monkeypatch.context() as patch:
            if not decided:
                patch.setattr(store, "commit_session", interrupt)
            with pytest.raises(OSError, match="decision"):
                if boundary == "start":
                    runtime._start_queued_run(
                        session=session,
                        run=run,
                        snapshot=snapshot,
                        inputs=list(inputs),
                    )
                else:
                    runtime._consume_queued_turn_inputs(
                        session=session,
                        run=run,
                        inputs=inputs,
                        turn_id="turn-consume",
                    )
                if decided:
                    raise OSError("interrupted after durable decision")
        after = store.load_session(session.session_id)
        pending = runtime.queued_inputs.eligible_session(after.session)
        assert [item.queue_id for item in pending] == (
            [] if decided else [i.queue_id for i in inputs]
        )
        assert [item.text_content for item in after.messages] == (
            ["first", "second"] if decided else []
        )
        assert all(
            item.before_harness_version == session.harness_version for item in after.messages
        )
        assert len(after.runs) == int(decided or boundary == "steer")
        notifications = [
            event
            for event in recorded_events(after.session.session_id)
            if event.event_type in {"plugin_message_added", "queued_input_consumed"}
        ]
        assert len(notifications) == (len(inputs) if decided else 0)
        assert [event.payload["message_id"] for event in notifications] == [
            message.message_id for message in after.messages
        ]
        if decided:
            for item in inputs:
                assert (
                    store.find_queued_input_message(session.session_id, item.queue_id) is not None
                )
            assert runtime._consume_queued_turn_inputs(
                session=session,
                run=run,
                inputs=inputs,
                turn_id="turn-consume",
            ) == ([], ())
    finally:
        await runtime.close()


@pytest.mark.parametrize("winner", ["cancel", "consume"])
async def test_cancel_and_consume_compete_on_the_exact_queue_identity(store, winner):
    runtime = runtime_for(store)
    session = runtime.create_session(title="Queue race")
    _, item, _, _ = runtime.queued_inputs.enqueue(
        snapshot=store.load_runtime_session(session.session_id),
        prompt="edit me",
        title=None,
        role="user",
        scaffold=False,
    )
    snapshot = store.load_runtime_session(session.session_id)
    entered, release = Event(), Event()

    def decide(action, first=False):
        with store.session_mutation(session.session_id):
            if first:
                entered.set()
                assert release.wait(5)
            if action == "cancel":
                return runtime.queued_inputs.cancel_user_input(
                    session_id=session.session_id,
                    queue_id=item.queue_id,
                )
            try:
                return runtime._start_queued_run(
                    session=session,
                    run=RunRecord(session_id=session.session_id),
                    snapshot=snapshot,
                    inputs=[item],
                )
            except _QueuedInputSelectionChanged:
                return None

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(decide, winner, True)
            assert entered.wait(5)
            second = executor.submit(decide, "consume" if winner == "cancel" else "cancel")
            release.set()
            assert first.result() is not None
            assert second.result() is None
        message = store.find_queued_input_message(session.session_id, item.queue_id)
        assert (message is not None) == (winner == "consume")
        assert (
            runtime.queued_inputs.eligible_session(store.load_session_record(session.session_id))
            == []
        )
    finally:
        release.set()
        await runtime.close()


@pytest.mark.parametrize("withdraw", [False, True])
@pytest.mark.parametrize("route", ["matching", "other-provider", "other-model"])
async def test_provider_artifact_budget_and_replay_require_current_route_and_epoch(
    store, withdraw, route
):
    runtime = runtime_for(store)
    try:
        first = await runtime.run("task")
        artifact = ProviderContinuationArtifact(
            provider_id="other" if route == "other-provider" else "faux",
            protocol="test",
            model_id="other" if route == "other-model" else "test",
            blocks=({"opaque": "state" if route == "matching" else "x" * 600_000},),
        )
        response = MessageRecord.text(
            session_id=first.session.session_id,
            run_id=first.run.run_id,
            role="assistant",
            text="retained work",
        ).model_copy(update={"provider_continuation": artifact})
        boundary = MessageRecord.text(
            session_id=first.session.session_id,
            run_id=first.run.run_id,
            role="user",
            text="withdraw this",
        )
        committed = store.commit_session(
            SessionCommit(
                before=store.load_session_record(first.session.session_id),
                steps=(SessionCommitStep(message=response), SessionCommitStep(message=boundary)),
            )
        )
        store.save_run(first.run.model_copy(update={"status": "aborted"}))
        if withdraw:
            store.truncate_messages(
                first.session.session_id,
                before_message_id=boundary.message_id,
                expected_last_message_id=boundary.message_id,
            )
        result = await runtime.continue_run(session_id=first.session.session_id)
        assert result.run.status == "completed"
        canonical = store.load_message(first.session.session_id, response.message_id)
        assert canonical.as_fact() == committed.messages[0]
        assert canonical.provider_continuation == artifact
        context = store.load_latest_context(first.session.session_id)
        projected = next(
            m
            for m in context.provider_context.messages
            if m.role == response.role and m.text_content == response.text_content
        )
        assert projected.provider_continuation == (
            artifact if not withdraw and route == "matching" else None
        )
        assert not context.metrics.over_budget
    finally:
        await runtime.close()


async def test_retained_user_queue_rechecks_policy_and_old_automatic_batch_cannot_drain(store):
    runtime = runtime_for(store)
    try:
        first = await runtime.run("original")
        _, user, _, _ = runtime.queued_inputs.enqueue(
            snapshot=store.load_runtime_session(first.session.session_id),
            prompt="pending user",
            title=None,
            role="user",
            scaffold=False,
            prompt_policy_checked=True,
            prospective_run_id="old_permit",
        )
        automatic = queued_batch(runtime, first.session)
        store.truncate_messages(
            first.session.session_id,
            before_message_id=first.session.last_message_id,
            expected_last_message_id=first.session.last_message_id,
        )
        pending = runtime.queued_inputs.eligible_session(
            store.load_session_record(first.session.session_id)
        )
        assert len(pending) == 1
        assert pending[0].queue_id == user.queue_id
        assert pending[0].prompt == user.prompt
        assert pending[0].history_epoch == 1
        assert not pending[0].prompt_policy_checked
        assert pending[0].prospective_run_id is None
        result = await runtime.run("new explicit input", session_id=first.session.session_id)
        assert result.run.status == "completed"
        consumed = store.find_queued_input_message(first.session.session_id, user.queue_id)
        assert consumed.text_content == "pending user"
        assert consumed.run_id != "old_permit"
        assert all(
            store.find_queued_input_message(first.session.session_id, item.queue_id) is None
            for item in automatic
        )
    finally:
        await runtime.close()
