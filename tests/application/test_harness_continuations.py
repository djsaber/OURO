from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest

from ouro.application.inputs.queue import QueuedInputQueue
from ouro.application.runs.continuations import RunContinuationService
from ouro.core.continuations import RunContinuationRegistry
from ouro.core.errors import PersistenceError
from ouro.core.session_controls import SessionControlActivation, SessionControlState
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import (
    RunContinuationCheckpoint,
    RunContinuationControllerRef,
    RunRecord,
    SessionRecord,
)
from ouro.plugins.inprocess.contributions import adapt_sdk_contributions
from ouro.plugins.session_controllers import PluginSessionControllerRegistry
from ouro.sdk.plugins import v5 as sdk
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from tests.harness_state import replace_harness_state

OWNER = "example.continuation"
CONTRACT = "c" * 64


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    store = (
        LocalSessionStore(tmp_path / "files")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "db")
    )
    store.initialize()
    return store


def setup(store, action):
    activation = SessionControlActivation(
        controller_id=f"{OWNER}/session_controllers/mode",
        controller_local_id="mode",
        plugin_id=OWNER,
        plugin_class="user",
        release_id="release",
        generation_id="generation",
        execution_contract_hash=CONTRACT,
    )
    session = SessionRecord(controls=SessionControlState(revision=1, activations=[activation]))
    store.create_session(session)
    replace_harness_state(
        store,
        SessionExtensionStateRecord(
            session_id=session.session_id, owner_id=OWNER, state_schema_version=1, payload="before"
        ),
    )
    run = RunRecord(
        session_id=session.session_id,
        status="completed",
        continuation=RunContinuationCheckpoint(
            controllers=[
                RunContinuationControllerRef(
                    plugin_id=OWNER,
                    policy_id="next",
                    version="1",
                    generation_id="generation",
                    release_id="release",
                    package_digest="release",
                    execution_contract_hash=CONTRACT,
                    contract_hash=CONTRACT,
                    active_when_controller_id=activation.controller_id,
                    activation_id=activation.activation_id,
                )
            ]
        ),
    )
    store.save_run(run)
    calls = []

    def decide(outcome):
        calls.append("policy")
        assert outcome.session_state.read().payload == "before"
        outcome.session_state.replace(state_schema_version=1, payload="policy")
        if action == "continue":
            return sdk.ContinuationDecision(action="continue", prompt="next input")
        return sdk.ContinuationDecision(
            action="complete",
            session_control_transitions=(sdk.SessionControlTransition("release", "mode"),),
        )

    def deactivate(call):
        calls.append("controller")
        assert call.session_state.read().payload == "policy"
        call.session_state.replace(state_schema_version=1, payload="controller")
        return sdk.SessionControllerOutput()

    contributions = adapt_sdk_contributions(
        sdk.ContributionSet(
            continuation_policies=[sdk.ContinuationPolicyContribution(id="next", handler=decide)],
            session_controllers=[
                sdk.SessionControllerContribution(
                    id="mode",
                    description="mode",
                    claims=(sdk.SessionControlClaim("example.mode"),),
                    activate=lambda call: sdk.SessionControllerOutput(),
                    deactivate=deactivate,
                )
            ],
        ),
        owner_id=OWNER,
    )
    policies = RunContinuationRegistry()
    policies.replace_owner(
        "instance",
        (
            replace(
                contributions.continuation_policies[0],
                plugin_id=OWNER,
                generation_id="generation",
                execution_contract_hash=CONTRACT,
                contract_hash=CONTRACT,
                active_when_controller_id=activation.controller_id,
            ),
        ),
    )
    controllers = PluginSessionControllerRegistry()
    controllers.replace_owner(
        "instance",
        (
            replace(
                contributions.session_controllers[0],
                plugin_id=OWNER,
                release_id="release",
                generation_id="generation",
                execution_contract_hash=CONTRACT,
            ),
        ),
    )
    service = RunContinuationService(
        store=store,
        registry=policies,
        queue=QueuedInputQueue(store),
        session_controllers=controllers,
    )
    return service, run, calls


@pytest.mark.parametrize("action", ["continue", "complete"])
@pytest.mark.parametrize("fault", [None, "before", "after"])
async def test_continuation_recovery_accepts_state_queue_and_checkpoint_without_callbacks(
    store, monkeypatch, action, fault
):
    service, run, calls = setup(store, action)
    before = store.load_harness(run.session_id)
    commit = store.commit_session

    def interrupted(request):
        if request.operation_id != run.continuation.decision_id:
            return commit(request)
        if fault == "before":
            raise PersistenceError("before decision")
        result = commit(request)
        if fault == "after":
            raise PersistenceError("reply lost")
        return result

    with monkeypatch.context() as boundary:
        boundary.setattr(store, "commit_session", interrupted)
        if fault == "before":
            with pytest.raises(PersistenceError, match="before decision"):
                await service.settle(session_id=run.session_id, run_id=run.run_id)
            assert store.load_harness(run.session_id) == before
            pending = store.load_run(run.session_id, run.run_id).continuation
            assert pending.state == "pending" and pending.settlement_plan is not None
            assert not service.queue.eligible_session(store.load_session_record(run.session_id))
        else:
            result = await service.settle(session_id=run.session_id, run_id=run.run_id)
            assert result.settled

    await service.settle(session_id=run.session_id, run_id=run.run_id)
    again = await service.settle(session_id=run.session_id, run_id=run.run_id)
    assert not again.settled
    assert calls == (["policy"] if action == "continue" else ["policy", "controller"])
    state = store.load_harness(run.session_id).state
    assert state.states[0].payload == ("policy" if action == "continue" else "controller")
    assert bool(state.controls.activations) == (action == "continue")
    current = store.load_session_record(run.session_id)
    queued = service.queue.eligible_session(current)
    assert [item.queue_id for item in queued] == (
        [run.continuation.queue_id] if action == "continue" else []
    )
    persisted = store.load_run(run.session_id, run.run_id)
    assert persisted.continuation.state == "resolved" and persisted.continuation.action == action


async def test_cancel_after_continuation_commit_reports_the_accepted_result(store, monkeypatch):
    service, run, calls = setup(store, "complete")
    commit = store.commit_session
    decided, release = threading.Event(), threading.Event()

    def held_reply(request):
        result = commit(request)
        if request.operation_id == run.continuation.decision_id:
            decided.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(store, "commit_session", held_reply)
    task = asyncio.create_task(service.settle(session_id=run.session_id, run_id=run.run_id))
    try:
        assert await asyncio.to_thread(decided.wait, 5)
        task.cancel()
    finally:
        release.set()
    result = await task
    assert result.settled and result.run.continuation.action == "complete"
    assert calls == ["policy", "controller"]
    assert not store.load_session_record(run.session_id).controls.activations


async def test_staged_continuation_cannot_overwrite_newer_state(store, monkeypatch):
    service, run, calls = setup(store, "complete")
    commit = store.commit_session

    def interrupt(request):
        if request.operation_id == run.continuation.decision_id:
            raise PersistenceError("before decision")
        return commit(request)

    with monkeypatch.context() as boundary:
        boundary.setattr(store, "commit_session", interrupt)
        with pytest.raises(PersistenceError):
            await service.settle(session_id=run.session_id, run_id=run.run_id)
    replace_harness_state(
        store,
        SessionExtensionStateRecord(
            session_id=run.session_id, owner_id=OWNER, state_schema_version=1, payload="newer"
        ),
    )
    newer = store.load_harness(run.session_id)
    result = await service.settle(session_id=run.session_id, run_id=run.run_id)
    assert result.run.continuation.reason == "continuation_state_changed"
    assert store.load_harness(run.session_id) == newer
    assert calls == ["policy", "controller"]


@pytest.mark.parametrize("conflict", [False, True])
async def test_failed_or_competing_policies_discard_every_owner_proposal(store, conflict):
    service, run, _ = setup(store, "continue")
    other_owner = "example.other"

    def decide(outcome):
        outcome.session_state.replace(state_schema_version=1, payload="must be discarded")
        if not conflict:
            raise ValueError("policy failed")
        return sdk.ContinuationDecision(action="continue", prompt="other")

    contribution = adapt_sdk_contributions(
        sdk.ContributionSet(
            continuation_policies=[sdk.ContinuationPolicyContribution(id="next", handler=decide)]
        ),
        owner_id=other_owner,
    ).continuation_policies[0]
    service.registry.replace_owner(
        "other",
        (
            replace(
                contribution,
                plugin_id=other_owner,
                generation_id="generation",
                execution_contract_hash=CONTRACT,
                contract_hash=CONTRACT,
            ),
        ),
    )
    other = run.continuation.controllers[0].model_copy(
        update={"plugin_id": other_owner, "active_when_controller_id": None, "activation_id": None}
    )
    run = run.model_copy(
        update={
            "continuation": run.continuation.model_copy(
                update={"controllers": [*run.continuation.controllers, other]}
            )
        }
    )
    store.save_run(run)
    before = store.load_harness(run.session_id)
    result = await service.settle(session_id=run.session_id, run_id=run.run_id)
    assert result.run.continuation.action == "pause"
    assert result.run.continuation.reason == (
        "continuation_policy_conflict" if conflict else "continuation_policy_failed"
    )
    assert store.load_harness(run.session_id) == before
    assert not service.queue.eligible_session(store.load_session_record(run.session_id))
