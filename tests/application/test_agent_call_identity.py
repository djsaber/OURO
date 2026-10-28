from __future__ import annotations

import asyncio
from threading import Event as ThreadEvent
from threading import get_ident

import pytest

from ouro.application.runs.lifecycle import request_run_abort_state
from ouro.application.sessions.editing import undo_user_input
from ouro.core.agents import AgentWaitResolution
from ouro.core.errors import PersistenceError
from ouro.core.provider import ProviderStreamEvent
from ouro.core.tool_facts import ToolCallIdentity
from tests.application.helpers import wait_for_parent_delivery
from tests.application.test_agent_multi_host import application


class ReusedCallProvider:
    provider_id = "test"

    def __init__(self, effects, *, delegations=2, execution_mode="sync"):
        self.effects = effects
        self.delegations = delegations
        self.execution_mode = execution_mode
        self.children = 0
        self.entered = [asyncio.Event(), asyncio.Event()]
        self.release = [asyncio.Event(), asyncio.Event()]

    async def stream(self, *, model, messages, system_prompt="", tools=None, request_context=None):
        if any(tool["name"] == "SpawnAgent" for tool in tools or []):
            results = [message for message in messages if message.role == "tool_result"]
            if len(results) < self.delegations:
                yield ProviderStreamEvent.from_tool_call(
                    {
                        "id": "reused",
                        "name": "SpawnAgent",
                        "arguments": {
                            "agent_id": "general",
                            "task": f"Independent child {len(results)}",
                            "description": f"Child {len(results)}",
                            "execution_mode": self.execution_mode,
                        },
                    }
                )
            else:
                yield ProviderStreamEvent(type="text_delta", text="Both children observed")
        else:
            index = self.children
            self.children += 1
            with self.effects.open("a", encoding="utf-8") as stream:
                stream.write(f"child {index}\n")
            self.entered[index].set()
            await self.release[index].wait()
            yield ProviderStreamEvent(type="text_delta", text=f"Distinct child result {index}")
        yield ProviderStreamEvent(type="done")


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("abort_parent", [False, True])
async def test_child_observation_precedes_parent_wait_parking(
    tmp_path, backend, abort_parent, monkeypatch
):
    app = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    provider = ReusedCallProvider(tmp_path / "external-children.txt", delegations=1)
    app.runtime.adapter.provider = provider
    dispatched, release, observed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    commit = app.agents._commit_spawn
    record = app.agents._record_wait_result
    finalize = app.agents._finalize_result_state

    def finalize_without_history(*args, **kwargs):
        def reject_history(*args, **kwargs):
            raise AssertionError("child result collection expanded Run history")

        with monkeypatch.context() as patch:
            patch.setattr(app.runtime.store, "load_run_messages", reject_history)
            return finalize(*args, **kwargs)

    monkeypatch.setattr(app.agents, "_finalize_result_state", finalize_without_history)

    async def hold_parent(*args, **kwargs):
        handle = await commit(*args, **kwargs)
        dispatched.set()
        await release.wait()
        return handle

    def record_child(relation):
        record(relation)
        if relation.result is not None:
            loop.call_soon_threadsafe(observed.set)

    monkeypatch.setattr(app.agents, "_commit_spawn", hold_parent)
    monkeypatch.setattr(app.agents, "_record_wait_result", record_child)
    async with app:
        parent = await app.supervisor.submit("Child finishes before parent wait parking")
        try:
            await asyncio.wait_for(dispatched.wait(), 60)
            await asyncio.wait_for(provider.entered[0].wait(), 60)
            store = app.runtime.store
            relation = store.list_delegations(parent.session_id)[0]
            identity = ToolCallIdentity(
                run_id=relation.parent_run_id,
                batch_id=relation.parent_batch_id,
                tool_call_id=relation.parent_tool_call_id,
            )
            before = store.load_tool_call_state(parent.session_id, identity)
            assert before.status == "running"
            assert before.attempt == 1
            assert before.agent_wait.child_run_id == relation.current_child_run_id
            assert before.agent_wait.delegation_id == relation.delegation_id
            provider.release[0].set()
            await asyncio.wait_for(observed.wait(), 60)
            outcome = store.load_tool_outcome(parent.session_id, identity, before.attempt)
            assert outcome is not None
            assert "Distinct child result 0" in outcome.value.content
            assert outcome.model_dump_json().count("Distinct child result 0") == 1
            assert "final_text" not in outcome.value.structured["result"]
            assert store.load_tool_call_state(parent.session_id, identity) == before
            assert store.load_run(parent.session_id, identity.run_id).status == "running"
            if abort_parent:
                await asyncio.to_thread(
                    request_run_abort_state,
                    store,
                    session_id=parent.session_id,
                    run_id=identity.run_id,
                    reason="Abort before parent parking",
                )
            release.set()
            if abort_parent:
                assert (await app.supervisor.wait(parent)).status == "aborted"
                await app.runtime.run_recovery.reconcile(
                    store.load_runtime_session(parent.session_id)
                )
                state = store.load_tool_call_state(parent.session_id, identity)
                assert state.agent_wait.wait_id == before.agent_wait.wait_id
                assert state.agent_wait.resolution.reason == "interrupted"
                assert state.outcome_attempt is None
            else:
                await wait_for_parent_delivery(app.supervisor, parent.session_id, identity.run_id)
                assert (
                    await app.supervisor.wait_run(parent.session_id, identity.run_id)
                ).status == "completed"
                state = store.load_tool_call_state(parent.session_id, identity)
                assert state.agent_wait.wait_id == before.agent_wait.wait_id
                assert state.outcome_attempt == before.attempt
            assert store.load_tool_outcome(parent.session_id, identity, before.attempt) == outcome
            assert provider.effects.read_text(encoding="utf-8").splitlines() == ["child 0"]
        finally:
            release.set()
            for child_release in provider.release:
                child_release.set()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("boundary", ["bind", "dispatch"])
async def test_spawn_rechecks_abort_before_binding_and_dispatch(
    tmp_path, backend, boundary, monkeypatch
):
    app = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    provider = ReusedCallProvider(tmp_path / "external-children.txt", delegations=1)
    app.runtime.adapter.provider = provider
    entered, release = asyncio.Event(), ThreadEvent()
    loop = asyncio.get_running_loop()
    name = "_bind_spawn" if boundary == "bind" else "_prepare_spawn_child"
    operation = getattr(app.agents, name)

    def hold(*args, **kwargs):
        if boundary == "dispatch":
            result = operation(*args, **kwargs)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(30), "spawn boundary was not released"
        return operation(*args, **kwargs) if boundary == "bind" else result

    monkeypatch.setattr(app.agents, name, hold)
    async with app:
        parent = await app.supervisor.submit("Abort before child dispatch")
        try:
            await asyncio.wait_for(entered.wait(), 60)
            relation = app.runtime.store.list_delegations(parent.session_id)[0]
            await asyncio.to_thread(
                request_run_abort_state,
                app.runtime.store,
                session_id=parent.session_id,
                run_id=relation.parent_run_id,
                reason="Abort during spawn preparation",
            )
            release.set()
            assert (await app.supervisor.wait(parent)).status == "aborted"
            assert not provider.effects.exists()
            assert provider.children == 0
            relation = app.runtime.store.list_delegations(parent.session_id)[0]
            if boundary == "bind":
                with pytest.raises(FileNotFoundError):
                    app.runtime.store.load_session_record(relation.child_session_id)
            else:
                child = app.runtime.store.load_run(
                    relation.child_session_id, relation.current_child_run_id
                )
                assert child.status == "aborted"
        finally:
            release.set()
            for child_release in provider.release:
                child_release.set()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_spawn_control_cannot_replace_started_arguments(tmp_path, backend, monkeypatch):
    app = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    provider = ReusedCallProvider(tmp_path / "external-children.txt", delegations=1)
    app.runtime.adapter.provider = provider
    execute = app.agents.execute_control

    async def changed_control(request, **kwargs):
        return await execute(
            request.model_copy(update={"task": "Unapproved replacement"}), **kwargs
        )

    monkeypatch.setattr(app.agents, "execute_control", changed_control)
    async with app:
        parent = await app.supervisor.submit("Keep the actual started invocation")
        result = await app.supervisor.wait(parent)
        assert result.status == "completed"
        assert not app.runtime.store.list_delegations(parent.session_id)
        assert not provider.effects.exists()
        messages = app.runtime.store.load_run_messages(parent.session_id, result.run_id)
        feedback = [message for message in messages if message.role == "tool_result"]
        assert len(feedback) == 1
        assert feedback[0].tool_result.is_error
        assert "started invocation" in feedback[0].text_content


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_reused_provider_call_id_has_distinct_delegations_and_delivery_receipts(
    tmp_path, backend, monkeypatch
):
    app = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    provider = ReusedCallProvider(tmp_path / "external-children.txt")
    app.runtime.adapter.provider = provider
    async with app:
        handle = await app.supervisor.submit("Delegate twice")
        try:
            await asyncio.wait_for(provider.entered[0].wait(), 60)
            provider.release[0].set()
            await asyncio.wait_for(provider.entered[1].wait(), 60)
            store = app.runtime.store
            relations = sorted(
                store.list_delegations(handle.session_id), key=lambda item: item.created_at
            )
            assert len(relations) == 2
            assert relations[0].parent_run_id == relations[1].parent_run_id
            assert {item.parent_tool_call_id for item in relations} == {"reused"}
            assert len({item.parent_batch_id for item in relations}) == 2
            assert len({item.idempotency_key for item in relations}) == 2
            identities = [
                ToolCallIdentity(
                    run_id=item.parent_run_id,
                    batch_id=item.parent_batch_id,
                    tool_call_id=item.parent_tool_call_id,
                )
                for item in relations
            ]
            first, second = [
                store.load_tool_call_state(handle.session_id, identity) for identity in identities
            ]
            assert first.result_message_id is not None
            assert second.status == "waiting_child"
            assert second.result_message_id is None
            with pytest.raises(ValueError, match="another native batch"):
                await app.supervisor.resume_agent_wait(
                    session_id=handle.session_id,
                    identity=identities[0],
                    resolution=AgentWaitResolution(
                        resolved_delegation_id=relations[0].delegation_id,
                        resolved_child_run_id=relations[0].current_child_run_id,
                        reason="condition_met",
                    ),
                )
            with pytest.raises(ValueError, match="requested delegation"):
                await app.runtime.resume_agent_wait(
                    session_id=handle.session_id,
                    identity=identities[1],
                    resolution=AgentWaitResolution(
                        resolved_delegation_id=relations[0].delegation_id,
                        resolved_child_run_id=relations[0].current_child_run_id,
                        reason="condition_met",
                    ),
                )
            assert store.load_tool_call_state(handle.session_id, identities[1]) == second
            with pytest.raises(ValueError, match="requested child Run"):
                await app.runtime.resume_agent_wait(
                    session_id=handle.session_id,
                    identity=identities[1],
                    resolution=AgentWaitResolution(
                        resolved_delegation_id=relations[1].delegation_id,
                        resolved_child_run_id=relations[0].current_child_run_id,
                        reason="condition_met",
                    ),
                )
            assert store.load_tool_call_state(handle.session_id, identities[1]) == second
            reads = []
            reader = get_ident()
            with monkeypatch.context() as patch:
                for name in (
                    "load_run",
                    "load_run_messages",
                    "load_tool_call",
                    "load_tool_outcome",
                    "load_tool_call_state",
                    "load_message_fact",
                ):
                    original = getattr(store, name)

                    def counted(*args, original=original, name=name, **kwargs):
                        if get_ident() == reader:
                            reads.append(name)
                        return original(*args, **kwargs)

                    patch.setattr(store, name, counted)
                for index, identity in enumerate(identities):
                    assert app.agents._tool_result_is_committed(
                        session_id=handle.session_id,
                        identity=identity,
                        delegation_id=relations[index].delegation_id,
                        child_run_id=relations[index].current_child_run_id,
                    ) is (index == 0)
            assert reads == ["load_tool_call_state", "load_message_fact", "load_tool_call_state"]
            with pytest.raises(PersistenceError, match="multiple Delegations"):
                app.agents.resolve_agent_navigation(
                    parent_session_id=handle.session_id,
                    parent_run_id=relations[0].parent_run_id,
                    parent_tool_call_id="reused",
                )
            selected = app.agents.resolve_agent_navigation(
                parent_session_id=handle.session_id,
                parent_run_id=relations[1].parent_run_id,
                parent_batch_id=relations[1].parent_batch_id,
                parent_tool_call_id="reused",
            )
            assert selected.delegation_id == relations[1].delegation_id
            provider.release[1].set()
            await wait_for_parent_delivery(
                app.supervisor, handle.session_id, relations[1].parent_run_id
            )
            completed = await app.supervisor.wait_run(handle.session_id, relations[1].parent_run_id)
            assert completed.status == "completed"
            messages = store.load_run_messages(handle.session_id, relations[1].parent_run_id)
            results = [message for message in messages if message.role == "tool_result"]
            assert len(results) == 2
            assert "Distinct child result 0" in results[0].text_content
            assert "Distinct child result 1" in results[1].text_content
            assert results[0].outcome_ref.identity == identities[0]
            assert results[1].outcome_ref.identity == identities[1]
            assert provider.effects.read_text(encoding="utf-8").splitlines() == [
                "child 0",
                "child 1",
            ]
        finally:
            for release in provider.release:
                release.set()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_sync_follow_up_result_is_delivered_to_parent(tmp_path, backend):
    app = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    provider = ReusedCallProvider(tmp_path / "external-children.txt", delegations=1)
    app.runtime.adapter.provider = provider
    async with app:
        parent = await app.supervisor.submit("Delegate, then accept a follow-up result")
        try:
            await asyncio.wait_for(provider.entered[0].wait(), 60)
            store = app.runtime.store
            first = store.list_delegations(parent.session_id)[0]
            identity = ToolCallIdentity(
                run_id=first.parent_run_id,
                batch_id=first.parent_batch_id,
                tool_call_id=first.parent_tool_call_id,
            )
            provider.release[0].set()
            await wait_for_parent_delivery(app.supervisor, parent.session_id, identity.run_id)
            await app.supervisor.wait_run(parent.session_id, identity.run_id)
            outcome = store.load_tool_outcome(parent.session_id, identity, 1)
            follow_up = await app.agents.send_agent_message(
                first.delegation_id,
                "Inspect again",
                request_id="follow-up",
                mode="follow_up",
                parent_session_id=parent.session_id,
            )
            assert follow_up.child_run_id != first.current_child_run_id
            assert follow_up.parent_batch_id == identity.batch_id
            await asyncio.wait_for(provider.entered[1].wait(), 60)
            provider.release[1].set()
            await asyncio.wait_for(app.agents.wait_agents((first.delegation_id,)), 60)
            relation = store.load_delegation(parent.session_id, first.delegation_id)
            assert relation.result.child_run_id == follow_up.child_run_id
            assert relation.result.delivery.policy == "next_boundary"
            assert relation.result.delivery.status == "delivered"
            receipt = relation.result.delivery
            if receipt.target_queue_id is not None:
                accepted = store.load_input(parent.session_id, receipt.target_queue_id)
            else:
                delivered = store.load_message_fact(parent.session_id, receipt.target_message_id)
                accepted = store.load_input(parent.session_id, delivered.input_ref.input_id)
            assert accepted.item.semantic_tag == "subagent-result"
            assert "Distinct child result 1" in accepted.item.prompt
            assert "Distinct child result 0" not in accepted.item.prompt
            for child_run_id, expected in (
                (first.current_child_run_id, True),
                (follow_up.child_run_id, False),
            ):
                assert (
                    app.agents._tool_result_is_committed(
                        session_id=parent.session_id,
                        identity=identity,
                        delegation_id=first.delegation_id,
                        child_run_id=child_run_id,
                    )
                    is expected
                )
            assert store.load_tool_outcome(parent.session_id, identity, 1) == outcome
            assert provider.effects.read_text(encoding="utf-8").splitlines() == [
                "child 0",
                "child 1",
            ]
        finally:
            for release in provider.release:
                release.set()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("interruption", ["error", "cancel"])
async def test_child_outcome_precedes_parent_callback_and_survives_interruption(
    tmp_path, backend, interruption, monkeypatch
):
    app = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    provider = ReusedCallProvider(tmp_path / "external-children.txt")
    app.runtime.adapter.provider = provider
    scheduled = asyncio.Event()
    resumed = []
    observed = []
    resume = app.supervisor.resume_agent_wait

    receipts = []
    own_receipt = app.agents._execution.own_receipt

    async def capture_resume(**kwargs):
        handle = await resume(**kwargs)
        resumed.append(handle)
        return handle

    def capture_receipt(handle):
        task = own_receipt(handle)
        if handle in resumed:
            receipts.append(task)
            scheduled.set()
        return task

    monkeypatch.setattr(app.supervisor, "resume_agent_wait", capture_resume)
    monkeypatch.setattr(app.agents._execution, "own_receipt", capture_receipt)
    async with app:
        parent = await app.supervisor.submit("Delegate once before extension failure")
        try:
            await asyncio.wait_for(provider.entered[0].wait(), 60)
            store = app.runtime.store
            relation = store.list_delegations(parent.session_id)[0]
            identity = ToolCallIdentity(
                run_id=relation.parent_run_id,
                batch_id=relation.parent_batch_id,
                tool_call_id=relation.parent_tool_call_id,
            )
            ensure = app.runtime._ensure_extension_snapshot

            def read_observation():
                # An independent worker can acquire the native mutation lock:
                # extension code runs only after the observation commit closes.
                with store.session_mutation(parent.session_id):
                    state = store.load_tool_call_state(parent.session_id, identity)
                    assert state.agent_wait.state == "resolved"
                    assert state.agent_wait.child_run_id == relation.current_child_run_id
                    assert state.agent_wait.resolution.resolved_child_run_id == (
                        relation.current_child_run_id
                    )
                    assert state.outcome_attempt == state.attempt == 1
                    assert state.result_message_id is None
                    assert store.load_run(parent.session_id, identity.run_id).status == "running"
                    outcome = store.load_tool_outcome(parent.session_id, identity, 1)
                    assert "Distinct child result 0" in outcome.value.content
                    return outcome

            async def failing_parent_extension(run):
                if run.run_id != identity.run_id:
                    return await ensure(run)
                observed.append(await asyncio.wait_for(asyncio.to_thread(read_observation), 30))
                if interruption == "cancel":
                    asyncio.current_task().cancel()
                    await asyncio.Event().wait()
                raise RuntimeError("parent extension failed after observing child")

            monkeypatch.setattr(app.runtime, "_ensure_extension_snapshot", failing_parent_extension)
            provider.release[0].set()
            await asyncio.wait_for(scheduled.wait(), 60)
            finished = await asyncio.wait_for(asyncio.shield(receipts[0]), 60)
            assert finished.status == ("failed" if interruption == "error" else "recoverable")
            assert len(observed) == 1
            assert store.load_tool_outcome(parent.session_id, identity, 1) == observed[0]
            monkeypatch.setattr(app.runtime, "_ensure_extension_snapshot", ensure)
            snapshot = await app.runtime.reconcile_execution_session(parent.session_id)
            await app.runtime.run_recovery.reconcile(snapshot)
            state = store.load_tool_call_state(parent.session_id, identity)
            message = store.load_message(parent.session_id, state.result_message_id)
            assert message.outcome_ref.identity == identity
            assert message.outcome_ref.outcome_attempt == 1
            assert "Distinct child result 0" in message.text_content
            assert not message.tool_result.is_error
            assert store.load_tool_outcome(parent.session_id, identity, 1) == observed[0]
            assert provider.effects.read_text(encoding="utf-8").splitlines() == ["child 0"]
        finally:
            for release in provider.release:
                release.set()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_child_observation_survives_abort_between_resume_read_and_commit(
    tmp_path, backend, monkeypatch
):
    app = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    provider = ReusedCallProvider(tmp_path / "external-children.txt", delegations=1)
    app.runtime.adapter.provider = provider
    entered, scheduled = asyncio.Event(), asyncio.Event()
    release = ThreadEvent()
    resumed = []
    callbacks = []
    loop = asyncio.get_running_loop()
    resume = app.supervisor.resume_agent_wait

    receipts = []
    own_receipt = app.agents._execution.own_receipt

    async def capture_resume(**kwargs):
        handle = await resume(**kwargs)
        resumed.append(handle)
        return handle

    def capture_receipt(handle):
        task = own_receipt(handle)
        if handle in resumed:
            receipts.append(task)
            scheduled.set()
        return task

    monkeypatch.setattr(app.supervisor, "resume_agent_wait", capture_resume)
    monkeypatch.setattr(app.agents._execution, "own_receipt", capture_receipt)
    async with app:
        parent = await app.supervisor.submit("Delegate before cancellation")
        try:
            await asyncio.wait_for(provider.entered[0].wait(), 60)
            store = app.runtime.store
            relation = store.list_delegations(parent.session_id)[0]
            identity = ToolCallIdentity(
                run_id=relation.parent_run_id,
                batch_id=relation.parent_batch_id,
                tool_call_id=relation.parent_tool_call_id,
            )
            save = app.runtime.run_lifecycle.save_batch_state
            ensure = app.runtime._ensure_extension_snapshot

            def held_commit(**kwargs):
                if kwargs["run"].run_id == identity.run_id and any(
                    call.agent_wait is not None and call.agent_wait.state == "resolved"
                    for call in kwargs["batch"].calls
                ):
                    loop.call_soon_threadsafe(entered.set)
                    assert release.wait(30), "child observation commit was not released"
                return save(**kwargs)

            async def counted_extension(run):
                if run.run_id == identity.run_id:
                    callbacks.append(run.run_id)
                return await ensure(run)

            monkeypatch.setattr(app.runtime.run_lifecycle, "save_batch_state", held_commit)
            monkeypatch.setattr(app.runtime, "_ensure_extension_snapshot", counted_extension)
            provider.release[0].set()
            await asyncio.wait_for(entered.wait(), 60)
            await asyncio.wait_for(scheduled.wait(), 60)
            _, requested, changed = await asyncio.to_thread(
                request_run_abort_state,
                store,
                session_id=parent.session_id,
                run_id=identity.run_id,
                reason="cancel before child observation commit",
            )
            assert changed and requested.abort_requested
            release.set()
            await asyncio.wait_for(asyncio.shield(receipts[0]), 60)
            await app.runtime.settle_run_abort(session_id=parent.session_id, run_id=identity.run_id)
            outcome = store.load_tool_outcome(parent.session_id, identity, 1)
            assert outcome is not None
            assert "Distinct child result 0" in outcome.value.content
            assert not callbacks
            assert store.load_run(parent.session_id, identity.run_id).status == "aborted"
            await app.runtime.run_recovery.reconcile(store.load_runtime_session(parent.session_id))
            state = store.load_tool_call_state(parent.session_id, identity)
            assert state.status == "unknown_outcome"
            assert state.outcome_attempt is None
            message = store.load_message(parent.session_id, state.result_message_id)
            assert message.outcome_ref.outcome_attempt is None
            assert "outcome is unknown" in message.text_content
            assert store.load_tool_outcome(parent.session_id, identity, 1) == outcome
            assert provider.effects.read_text(encoding="utf-8").splitlines() == ["child 0"]
        finally:
            release.set()
            for child_release in provider.release:
                child_release.set()


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("withdraw", [False, True])
async def test_child_result_is_retained_after_parent_history_is_closed(
    tmp_path, backend, withdraw, monkeypatch
):
    app = application(tmp_path, backend, "C:/PowerShell/pwsh.exe")
    provider = ReusedCallProvider(tmp_path / "external-children.txt", delegations=1)
    app.runtime.adapter.provider = provider
    entered, recorded = asyncio.Event(), asyncio.Event()
    release = ThreadEvent()
    loop = asyncio.get_running_loop()
    callbacks = []
    async with app:
        parent = await app.supervisor.submit("Delegate before closing the parent history")
        try:
            await asyncio.wait_for(provider.entered[0].wait(), 60)
            assert (await app.supervisor.wait(parent)).status == "waiting_child"
            store = app.runtime.store
            relation = store.list_delegations(parent.session_id)[0]
            identity = ToolCallIdentity(
                run_id=relation.parent_run_id,
                batch_id=relation.parent_batch_id,
                tool_call_id=relation.parent_tool_call_id,
            )
            record = app.agents._record_wait_result
            ensure = app.runtime._ensure_extension_snapshot

            def held_record(observed):
                if observed.delegation_id == relation.delegation_id and observed.result is not None:
                    loop.call_soon_threadsafe(entered.set)
                    assert release.wait(30), "late child observation was not released"
                record(observed)
                if observed.delegation_id == relation.delegation_id and observed.result is not None:
                    loop.call_soon_threadsafe(recorded.set)

            async def counted_extension(run):
                if run.run_id == identity.run_id:
                    callbacks.append(run.run_id)
                return await ensure(run)

            monkeypatch.setattr(app.agents, "_record_wait_result", held_record)
            monkeypatch.setattr(app.runtime, "_ensure_extension_snapshot", counted_extension)
            provider.release[0].set()
            await asyncio.wait_for(entered.wait(), 60)
            assert store.load_tool_outcome(parent.session_id, identity, 1) is None
            await app.runtime.abort_run(session_id=parent.session_id, run_id=identity.run_id)
            await app.runtime.run_recovery.reconcile(store.load_runtime_session(parent.session_id))
            state = store.load_tool_call_state(parent.session_id, identity)
            unknown = store.load_message_fact(parent.session_id, state.result_message_id)
            assert unknown.outcome_ref.outcome_attempt is None
            if withdraw:
                await asyncio.to_thread(undo_user_input, store, parent.session_id)
            before = store.load_session(parent.session_id)
            release.set()
            await asyncio.wait_for(recorded.wait(), 60)
            await app.agents._finalize_result(relation.delegation_id)
            outcome = store.load_tool_outcome(parent.session_id, identity, 1)
            assert outcome is not None
            assert outcome.value.structured["result"]["terminal_status"] == "completed"
            assert (
                outcome.value.structured["handle"]["child_run_id"] == relation.current_child_run_id
            )
            assert "Distinct child result 0" in outcome.value.content
            assert not callbacks
            await asyncio.wait_for(
                asyncio.gather(
                    *tuple(app.agents._watchers),
                    *tuple(app.agents._execution._tasks.values()),
                ),
                60,
            )
            # Closing changes the mutable handle's revision/control state. A
            # repeated settlement must retain the observation already recorded.
            closed = await app.agents.close_agent(relation.delegation_id)
            assert closed.control_state == "closed"
            await app.agents._finalize_result(relation.delegation_id)
            assert store.load_tool_outcome(parent.session_id, identity, 1) == outcome
            after = store.load_session(parent.session_id)
            assert after.messages == before.messages
            assert after.session.history_epoch == before.session.history_epoch
            assert after.session.harness_version == before.session.harness_version
            assert after.session.active_run_id is None
            assert store.load_run(parent.session_id, identity.run_id).status == "aborted"
            if not withdraw:
                assert (
                    store.load_message_fact(parent.session_id, state.result_message_id) == unknown
                )
            else:
                assert not after.messages
            assert not app.agents._tool_result_is_committed(
                session_id=parent.session_id,
                identity=identity,
                delegation_id=relation.delegation_id,
                child_run_id=relation.current_child_run_id,
            )
            assert provider.effects.read_text(encoding="utf-8").splitlines() == ["child 0"]
        finally:
            release.set()
            for child_release in provider.release:
                child_release.set()
