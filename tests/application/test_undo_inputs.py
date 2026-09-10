import asyncio
from threading import Event

import pytest

from ouro.application.runtime import RuntimeService
from ouro.application.supervisor import RuntimeSupervisor
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import PersistenceError, SessionBusyError
from ouro.core.messages import MessageRecord, SkillActivationContent, TextContent
from ouro.core.provider import ModelMetadata
from ouro.core.session_commits import SessionCommit, SessionCommitConflict
from ouro.core.session_controls import HostSessionInput, SessionInputRequest
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import RunRecord
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry


@pytest.fixture(params=["files", "sqlite"])
async def runtime(tmp_path, request):
    store = (
        LocalSessionStore(tmp_path / "sessions")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(FauxProvider(), ModelMetadata(model_id="test", provider_id="faux")),
        tools=ToolRegistry(),
    )
    yield runtime
    await runtime.close()


def enqueue(runtime, session_id, prompt, **kwargs):
    return runtime.queued_inputs.enqueue(
        snapshot=runtime.store.load_runtime_session(session_id),
        prompt=prompt,
        title=None,
        role="user",
        scaffold=False,
        **kwargs,
    )[1]


async def test_undo_restores_owner_state_and_preserves_independent_pending_inputs(runtime):
    store = runtime.store
    first = await runtime.run("keep this")
    session_id = first.session.session_id
    state = SessionExtensionStateRecord(
        session_id=session_id,
        owner_id="test.owner",
        state_schema_version=1,
        payload={"value": 1},
    )
    store.save_session_extension_state(state)
    second = await runtime.run("edit this", session_id=session_id)
    target = next(
        m for m in store.load_run_messages(session_id, second.run.run_id) if m.role == "user"
    )
    store.save_session_extension_state(state.model_copy(update={"payload": {"value": 2}}))
    user = enqueue(
        runtime, session_id, "later", prompt_policy_checked=True, prospective_run_id="old"
    )
    host = HostSessionInput(prompt="independent host", host_source_id="test.host")
    receipt = await runtime.submit_host_input(host, session_id=session_id)
    _, automatic, _, _ = runtime.queued_inputs.enqueue_batch(
        snapshot=store.load_runtime_session(session_id),
        inputs=(SessionInputRequest(prompt="automatic"),),
        source="plugin",
        plugin_id="test.owner",
        contribution_id="command",
        generation_id="g",
        execution_contract_hash="contract",
    )
    result = await runtime.undo(session_id, message_id=target.message_id)
    assert result.input.content == target.content
    assert result.session.history_epoch == 1
    assert store.load_harness(session_id).state.states == (state,)
    pending = runtime.queued_inputs.eligible_session(result.session)
    assert [item.queue_id for item in pending] == [user.queue_id, receipt.queue_id]
    assert all(item.history_epoch == 1 and not item.prompt_policy_checked for item in pending)
    assert result.cancelled_queue_ids == (automatic[0].queue_id,)
    assert [m.text_content for m in store.load_runtime_session(session_id).messages] == [
        "keep this",
        "Hello from OURO.",
    ]


async def test_default_undo_cancels_last_queued_input_during_live_execution(runtime, tmp_path):
    session = runtime.create_session(title="Live queue")
    session, run = runtime.run_lifecycle.start(
        session=session,
        run=RunRecord(session_id=session.session_id),
        snapshot=runtime.store.load_runtime_session(session.session_id),
    )
    first = enqueue(runtime, session.session_id, "keep queued")
    skill = SkillActivationContent(
        name="reviewer",
        source="workspace",
        location=str(tmp_path / "SKILL.md"),
        base_directory=str(tmp_path),
        instructions="Review carefully",
        content_digest=f"sha256:{'a' * 64}",
    )
    last = enqueue(
        runtime, session.session_id, "edit", skill_activation=skill, resource_refs=("resource",)
    )
    lock = runtime.store.acquire_session_lock(session.session_id, run.run_id)
    try:
        result = await runtime.undo(session.session_id)
        assert result.queue_id == last.queue_id
        assert result.removed_count == 0
        assert result.input.content == [skill, TextContent(text="edit")]
        assert result.resource_refs == ("resource",)
        assert result.session.history_epoch == 0
        assert result.session.active_run_id == run.run_id
        assert [i.queue_id for i in runtime.queued_inputs.eligible_session(result.session)] == [
            first.queue_id
        ]
    finally:
        lock.release()


async def test_consumed_queue_identity_never_cancels_or_selects_another_input(runtime):
    session = runtime.create_session(title="Consumed queue")
    consumed = enqueue(runtime, session.session_id, "consumed")
    snapshot = runtime.store.load_runtime_session(session.session_id)
    _, run, messages = runtime._start_queued_run(
        session=session,
        run=RunRecord(session_id=session.session_id),
        snapshot=snapshot,
        inputs=[consumed],
        existing_message_count=0,
    )
    pending = enqueue(runtime, session.session_id, "unrelated")
    lock = runtime.store.acquire_session_lock(session.session_id, run.run_id)
    try:
        with pytest.raises(SessionBusyError, match="Abort"):
            await runtime.undo(session.session_id, queue_id=consumed.queue_id)
    finally:
        lock.release()
    assert runtime.store.load_message(session.session_id, messages[0].message_id) is not None
    await runtime.abort_run(session_id=session.session_id, run_id=run.run_id)
    result = await runtime.undo(session.session_id, queue_id=consumed.queue_id)
    assert result.message_id == messages[0].message_id
    assert result.text_content == "consumed"
    assert [i.queue_id for i in runtime.queued_inputs.eligible_session(result.session)] == [
        pending.queue_id
    ]


async def test_app_undo_uses_provenance_and_returns_all_canonical_editor_data(runtime, tmp_path):
    first = await runtime.run("user")
    session_id = first.session.session_id
    foreign = MessageRecord.text(
        session_id=session_id, role="user", text="plugin input"
    ).model_copy(update={"metadata": {"session_input_source": "plugin"}})
    runtime.store.append_message(foreign)
    with pytest.raises(ValueError, match="directly submitted"):
        await runtime.undo(session_id, message_id=foreign.message_id)
    skill = SkillActivationContent(
        name="reviewer",
        source="workspace",
        location=str(tmp_path / "SKILL.md"),
        base_directory=str(tmp_path),
        instructions="Review",
        content_digest=f"sha256:{'b' * 64}",
    )
    direct = MessageRecord(
        session_id=session_id,
        role="user",
        content=[skill, TextContent(text="edit")],
        resource_refs=["attachment"],
        metadata={"session_input_source": "user"},
    )
    runtime.store.append_message(direct)
    result = await runtime.undo(session_id, message_id=direct.message_id)
    assert result.input.content == direct.content
    assert result.resource_refs == tuple(direct.resource_refs)
    assert runtime.store.load_message(session_id, foreign.message_id) is not None


async def test_undo_returns_durable_result_when_cancelled_after_decision(runtime, monkeypatch):
    first = await runtime.run("withdraw")
    entered, release = Event(), Event()
    commit = runtime.store.commit_session

    def gated(request):
        result = commit(request)
        if request.before_message_id is not None:
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(runtime.store, "commit_session", gated)
    task = asyncio.create_task(runtime.undo(first.session.session_id))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        result = await task
        assert result.text_content == "withdraw"
        assert result.session.history_epoch == 1
        assert runtime.store.load_runtime_session(first.session.session_id).messages == []
    finally:
        release.set()
        await task


async def test_undo_recovers_a_lost_native_commit_response(runtime, monkeypatch):
    first = await runtime.run("withdraw")
    commit = runtime.store.commit_session

    def lose_response(request):
        result = commit(request)
        if request.before_message_id is not None:
            raise PersistenceError("lost response")
        return result

    monkeypatch.setattr(runtime.store, "commit_session", lose_response)
    result = await runtime.undo(first.session.session_id)
    assert result.session.history_epoch == 1
    assert result.text_content == "withdraw"


async def test_host_retry_cannot_resurrect_a_consumed_withdrawn_input(runtime):
    session = runtime.create_session(title="Host acceptance")
    request = HostSessionInput(
        prompt="accepted work", host_source_id="test.host", queue_id="stable"
    )
    receipt = await runtime.submit_host_input(request, session_id=session.session_id)
    result = await runtime.consume_host_input(receipt)
    consumed = runtime.store.find_queued_input_message(session.session_id, "stable")
    runtime.store.commit_session(
        SessionCommit(
            before=runtime.store.load_session_record(session.session_id),
            before_message_id=consumed.message_id,
        )
    )
    with pytest.raises(ValueError, match="already accepted"):
        await runtime.submit_host_input(request, session_id=session.session_id)
    assert runtime.store.load_runtime_session(session.session_id).messages == []
    assert runtime.pending_user_inputs(session.session_id) == ()
    with pytest.raises(SessionCommitConflict, match="withdrawn"):
        await runtime.submit_host_input(
            request.model_copy(
                update={"queue_id": "late", "history_epoch": result.run.history_epoch}
            ),
            session_id=session.session_id,
        )


async def test_old_finalizer_cannot_drain_preserved_user_input_after_undo(runtime):
    first = await runtime.run("withdraw")
    target = next(
        m
        for m in runtime.store.load_run_messages(first.session.session_id, first.run.run_id)
        if m.role == "user"
    )
    pending = enqueue(runtime, first.session.session_id, "keep pending")
    await runtime.undo(first.session.session_id, message_id=target.message_id)
    await runtime._finalize_run_boundary(first, drain_queued_inputs=True)
    current = runtime.store.load_runtime_session(first.session.session_id)
    assert current.messages == []
    assert [i.queue_id for i in runtime.queued_inputs.eligible_session(current.session)] == [
        pending.queue_id
    ]
    with pytest.raises(SessionCommitConflict, match="withdrawn"):
        await runtime.continue_run(session_id=first.session.session_id, history_epoch=0)


async def test_supervised_continuation_rejects_history_changed_after_scheduling(
    runtime, monkeypatch
):
    first = await runtime.run("retained input")
    session_id = first.session.session_id
    runtime.store.commit_session(
        SessionCommit(
            before=runtime.store.load_session_record(session_id),
            before_message_id=first.session.last_message_id,
        )
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.continue_run

    async def held(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    monkeypatch.setattr(runtime, "continue_run", held)
    supervisor = RuntimeSupervisor(runtime, max_concurrency=1)
    try:
        handle = await supervisor.continue_run(session_id=session_id)
        await asyncio.wait_for(entered.wait(), timeout=10)
        await runtime.undo(session_id)
        release.set()
        status = await supervisor.wait(handle)
        assert status.status == "failed"
        after = runtime.store.load_runtime_session(session_id)
        assert not after.messages
        assert after.runs == [first.run]
        assert after.session.history_epoch == 2
    finally:
        release.set()
        await supervisor.shutdown()
