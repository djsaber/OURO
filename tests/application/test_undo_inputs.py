import asyncio
from threading import Event

import pytest

from ouro.application.inputs.requests import UserInputRequest, UserMessageEdit
from ouro.application.runtime.service import RuntimeService
from ouro.application.session_controls import SessionControlContractError
from ouro.application.supervisor import RuntimeSupervisor
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import PersistenceError, PromptRejectedError, SessionBusyError
from ouro.core.harness import HarnessState
from ouro.core.messages import MessageRecord, SkillActivationContent, TextContent
from ouro.core.prompt_policies import PromptPolicyDecision, PromptPolicyRegistration
from ouro.core.provider import ModelMetadata
from ouro.core.session_commits import SessionCommit, SessionCommitConflict, SessionCommitStep
from ouro.core.session_controls import (
    HostSessionInput,
    SessionControlActivation,
    SessionControlState,
    SessionInputRequest,
)
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.providers import FauxProvider
from ouro.sessions.backends.files.store import LocalSessionStore
from ouro.sessions.backends.sqlite.store import SQLiteSessionStore
from ouro.tools import ToolRegistry
from tests.harness_state import replace_harness_state
from tests.input_facts import queued_input


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
        session_id=session_id,
        item=queued_input(
            runtime.store.load_runtime_session(session_id).session,
            prompt=prompt,
            **kwargs,
        ),
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
    replace_harness_state(store, state)
    second = await runtime.run("edit this", session_id=session_id)
    target = next(
        m for m in store.load_run_messages(session_id, second.run.run_id) if m.role == "user"
    )
    replace_harness_state(store, state.model_copy(update={"payload": {"value": 2}}))
    user = enqueue(
        runtime, session_id, "later", prompt_policy_checked=True, prospective_run_id="old"
    )
    host = HostSessionInput(prompt="independent host", host_source_id="test.host")
    receipt = await runtime.submit_host_input(host, session_id=session_id)
    _, automatic, _, _, _ = runtime.queued_inputs.enqueue_batch(
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
    assert [m.text_content for m in store.load_session(session_id).messages] == [
        "keep this",
        "Hello from OURO.",
    ]


async def test_default_undo_cancels_last_queued_input_during_live_execution(runtime, tmp_path):
    session = runtime.create_session(title="Live queue")
    session, run, _ = runtime.run_lifecycle.start(
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
        runtime, session.session_id, "edit", skill_activations=(skill,), resource_refs=("resource",)
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


async def test_edit_checks_policy_at_cut_and_accepts_replacement_once(runtime):
    prefix = (await runtime.run("retained")).session
    session_id = prefix.session_id
    store = runtime.store
    state = SessionExtensionStateRecord(
        session_id=session_id, owner_id="guard", state_schema_version=1, payload={"step": 1}
    )
    replace_harness_state(store, state)
    original = await runtime.run("original", session_id=session_id)
    selected = next(
        m for m in store.load_run_messages(session_id, original.run.run_id) if m.role == "user"
    )
    replace_harness_state(store, state.model_copy(update={"payload": {"step": 2}}))
    pending = enqueue(runtime, session_id, "independent pending input")
    before = store.load_session(session_id)
    request = UserInputRequest(
        session_id=session_id,
        prompt="denied",
        edit=UserMessageEdit(
            message_id=selected.message_id, history_epoch=0, message_count=len(before.messages)
        ),
    )
    evaluations = []

    def policy(call):
        harness = call.harness
        evaluations.append((harness.harness_version, harness.last_message_id, harness.state.states))
        return (
            PromptPolicyDecision(action="deny", reason="denied")
            if call.prompt == "denied"
            else PromptPolicyDecision()
        )

    runtime.prompt_policies.register(PromptPolicyRegistration("test.owner", "guard", policy))
    with pytest.raises(PromptRejectedError):
        await runtime.accept_user_input(request)
    assert store.load_session(session_id) == before
    request = request.model_copy(update={"prompt": "replacement"})
    receipt = await runtime.accept_user_input(request)
    assert await runtime.accept_user_input(request) == receipt
    assert evaluations == [(selected.before_harness_version, prefix.last_message_id, (state,))] * 2
    current = store.load_session_record(session_id)
    assert current.history_epoch == 1
    assert current.message_count == prefix.message_count
    assert current.last_message_id == prefix.last_message_id
    assert current.pending_input_ids == (receipt.queue_id, pending.queue_id)
    assert store.load_input(session_id, receipt.queue_id).item.history_epoch == 1
    assert store.load_input_state(session_id, selected.input_id).status == "withdrawn"
    with pytest.raises(ValueError, match="already bound"):
        await runtime.accept_user_input(request.model_copy(update={"edit": None}))


async def test_stale_edit_never_cuts_the_new_suffix(runtime):
    first = await runtime.run("original")
    session = first.session
    selected = runtime.store.load_messages_tail(session.session_id)[-1]
    request = UserInputRequest(
        session_id=session.session_id,
        prompt="replacement",
        edit=UserMessageEdit(
            message_id=selected.message_id,
            history_epoch=session.history_epoch,
            message_count=session.message_count,
        ),
    )
    lock = runtime.store.acquire_session_lock(session.session_id, "live_owner")
    try:
        with pytest.raises(SessionBusyError, match="Abort"):
            await runtime.accept_user_input(request)
    finally:
        lock.release()
    await runtime.run("concurrent message", session_id=session.session_id)
    before = runtime.store.load_session(session.session_id)
    with pytest.raises(SessionCommitConflict, match="history changed"):
        await runtime.accept_user_input(request)
    assert runtime.store.load_session(session.session_id) == before


async def test_edit_cannot_overwrite_a_harness_change_during_admission(runtime):
    session = (await runtime.run("original")).session
    store = runtime.store
    before = store.load_session(session.session_id)
    state = SessionExtensionStateRecord(
        session_id=session.session_id, owner_id="concurrent", state_schema_version=1, payload={}
    )

    def policy(_call):
        replace_harness_state(store, state)
        return PromptPolicyDecision()

    runtime.prompt_policies.register(PromptPolicyRegistration("test.owner", "guard", policy))
    request = UserInputRequest(
        session_id=session.session_id,
        prompt="replacement",
        edit=UserMessageEdit(
            message_id=before.messages[0].message_id,
            history_epoch=session.history_epoch,
            message_count=session.message_count,
        ),
    )
    with pytest.raises(SessionCommitConflict, match="Harness state changed"):
        await runtime.accept_user_input(request)
    assert store.load_session(session.session_id).messages == before.messages
    assert store.load_harness(session.session_id).state.states == (state,)
    assert store.load_input(session.session_id, request.input_id) is None


async def test_edit_does_not_create_an_unbound_run_in_a_delegated_session(runtime):
    root = runtime.create_session()
    child = SessionRecord(
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id="delegated-task",
    )
    store = runtime.store
    store.create_session(child)
    message = MessageRecord.text(session_id=child.session_id, role="user", text="child input")
    message.metadata["session_input_source"] = "user"
    store.append_message(message)
    before = store.load_session(child.session_id)
    request = UserInputRequest(
        session_id=child.session_id,
        prompt="replacement",
        edit=UserMessageEdit(
            message_id=message.message_id,
            history_epoch=before.session.history_epoch,
            message_count=before.session.message_count,
        ),
    )
    with pytest.raises(ValueError, match="primary session"):
        await runtime.accept_user_input(request)
    assert store.load_session(child.session_id) == before


async def test_edit_requires_the_controller_at_the_selected_boundary(runtime):
    store = runtime.store
    session = runtime.create_session()
    guard = SessionControlActivation(
        controller_id="test.owner/session_controllers/guard",
        plugin_id="test.owner",
        controller_local_id="guard",
        plugin_class="user",
        release_id="release",
        generation_id="generation",
        execution_contract_hash="guard-contract",
    )
    message = MessageRecord.text(session_id=session.session_id, role="user", text="original")
    message.metadata["session_input_source"] = "user"
    before = store.commit_session(
        SessionCommit(
            before=session,
            steps=(
                SessionCommitStep(
                    harness=HarnessState(controls=SessionControlState(activations=[guard]))
                ),
                SessionCommitStep(message=message, harness=HarnessState()),
            ),
        )
    ).session
    with pytest.raises(SessionControlContractError, match="unavailable"):
        await runtime.accept_user_input(
            UserInputRequest(
                session_id=session.session_id,
                prompt="replacement",
                edit=UserMessageEdit(
                    message_id=message.message_id,
                    history_epoch=before.history_epoch,
                    message_count=before.message_count,
                ),
            )
        )
    assert store.load_session_record(session.session_id) == before


async def test_consumed_queue_identity_never_cancels_or_selects_another_input(runtime):
    wakeups = []

    async def schedule(session_id, history_epoch):
        wakeups.append((session_id, history_epoch))

    # Hold execution capacity while testing the identity-specific history operation.
    runtime.bind_session_scheduler(schedule)
    session = runtime.create_session(title="Consumed queue")
    consumed = enqueue(runtime, session.session_id, "consumed")
    snapshot = runtime.store.load_runtime_session(session.session_id)
    _, run, messages, _ = runtime._start_queued_run(
        session=session,
        run=RunRecord(session_id=session.session_id),
        snapshot=snapshot,
        inputs=[consumed],
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
    assert wakeups == [(session.session_id, session.history_epoch)]
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
        assert runtime.store.load_session(first.session.session_id).messages == []
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
    assert runtime.store.load_session(session.session_id).messages == []
    assert runtime.pending_user_inputs(session.session_id) == ()
    with pytest.raises(SessionCommitConflict, match="withdrawn"):
        await runtime.submit_host_input(
            request.model_copy(
                update={"queue_id": "late", "history_epoch": result.run.history_epoch}
            ),
            session_id=session.session_id,
        )


async def test_old_finalizer_cannot_schedule_preserved_user_input_after_undo(runtime):
    first = await runtime.run("withdraw")
    target = next(
        m
        for m in runtime.store.load_run_messages(first.session.session_id, first.run.run_id)
        if m.role == "user"
    )
    pending = enqueue(runtime, first.session.session_id, "keep pending")
    await runtime.undo(first.session.session_id, message_id=target.message_id)
    await runtime._finalize_run_boundary(first)
    current = runtime.store.load_session(first.session.session_id)
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
        after = runtime.store.load_session(session_id)
        assert not after.messages
        assert after.runs == [first.run]
        assert after.session.history_epoch == 2
    finally:
        release.set()
        await supervisor.shutdown()
