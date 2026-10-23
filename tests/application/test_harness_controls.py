from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from ouro.application.harness import capture_harness
from ouro.application.session_controls import SessionControlCoordinator, SessionControlError
from ouro.core.harness_calls import HarnessInvocation, accept_state_changes
from ouro.core.session_commits import SessionCommit, SessionCommitConflict, SessionCommitStep
from ouro.core.session_controls import AttributedSessionControlTransition
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import SessionRecord
from ouro.plugins.inprocess.contributions import adapt_sdk_contributions
from ouro.plugins.session_controllers import PluginSessionControllerRegistry
from ouro.sdk.plugins import v5 as sdk
from ouro.sessions import LocalSessionStore, SQLiteSessionStore

OWNER = "example.controls"
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


def registry(activate, deactivate, *, name="mode"):
    contributed = sdk.SessionControllerContribution(
        id=name,
        description="own state",
        claims=(sdk.SessionControlClaim("example.mode", "exclusive"),),
        activate=activate,
        deactivate=deactivate,
    )
    controller = adapt_sdk_contributions(
        sdk.ContributionSet(session_controllers=[contributed]), owner_id=OWNER
    ).session_controllers[0]
    result = PluginSessionControllerRegistry()
    result.replace_owner(
        "instance",
        (
            replace(
                controller,
                plugin_id=OWNER,
                release_id="release",
                generation_id="generation",
                execution_contract_hash=CONTRACT,
            ),
        ),
    )
    return result


def transition(action="acquire", **kwargs):
    return AttributedSessionControlTransition(
        action=action,
        controller_id="mode",
        plugin_id=OWNER,
        generation_id="generation",
        execution_contract_hash=CONTRACT,
        **kwargs,
    )


def deactivate(call):
    call.session_state.delete()
    return sdk.SessionControllerOutput()


async def test_controller_state_is_invisible_until_claims_commit(store):
    session = SessionRecord()
    store.create_session(session)
    entered, release = asyncio.Event(), asyncio.Event()

    async def activate(call):
        call.session_state.replace(state_schema_version=1, payload={"mode": "active"})
        entered.set()
        await release.wait()
        return sdk.SessionControllerOutput()

    coordinator = SessionControlCoordinator(store)
    task = asyncio.create_task(
        coordinator.apply(
            transition(),
            session_id=session.session_id,
            controllers=registry(activate, deactivate),
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        before = store.load_harness(session.session_id)
        assert before.version_id == session.harness_version
        assert not before.state.states
        assert not before.state.controls.activations
    finally:
        release.set()
    result = await task
    after = store.load_harness(session.session_id)
    assert after.state.states[0].payload == {"mode": "active"}
    assert after.state.controls.activations == [result.activation]


async def test_nested_controller_reads_and_extends_its_parents_uncommitted_state(store):
    session = SessionRecord()
    store.create_session(session)
    snapshot = capture_harness(store, session.session_id)
    parent = HarnessInvocation(snapshot, OWNER, writable=True)
    parent.replace(
        SessionExtensionStateRecord(
            session_id=session.session_id,
            owner_id=OWNER,
            state_schema_version=1,
            payload={"from_command": "draft"},
        )
    )
    draft = accept_state_changes(snapshot, [parent.finish()])

    def activate(call):
        value = call.session_state.read().payload
        assert value == {"from_command": "draft"}
        assert not store.load_harness(session.session_id).state.states
        call.session_state.replace(state_schema_version=1, payload={**value, "active": True})
        return sdk.SessionControllerOutput()

    coordinator = SessionControlCoordinator(store)
    draft, result = await coordinator.stage(
        transition(),
        session=session,
        draft=draft,
        controllers=registry(activate, deactivate),
    )
    assert not store.load_harness(session.session_id).state.states
    coordinator.commit(
        SessionCommit(before=session, steps=(SessionCommitStep(harness=draft.state),))
    )
    state = store.load_harness(session.session_id).state
    assert state.states[0].payload == {"from_command": "draft", "active": True}
    assert state.controls.activations == [result.activation]


@pytest.mark.parametrize("action", ["acquire", "release", "replace"])
async def test_failed_lifecycle_discards_all_nested_owner_changes(store, action):
    session = SessionRecord()
    store.create_session(session)

    def activate(call):
        call.session_state.replace(state_schema_version=1, payload={"active": True})
        return sdk.SessionControllerOutput()

    def fail(call):
        call.session_state.replace(state_schema_version=1, payload="must not be committed")
        raise ValueError("lifecycle failed")

    coordinator = SessionControlCoordinator(store)
    if action != "acquire":
        active = await coordinator.apply(
            transition(),
            session_id=session.session_id,
            controllers=registry(activate, deactivate),
        )
    before = store.load_harness(session.session_id)
    selected = transition(
        action,
        **(
            {"replace_activation_ids": [active.activation.activation_id]}
            if action == "replace"
            else {}
        ),
    )
    with pytest.raises(SessionControlError, match="lifecycle failed"):
        await coordinator.apply(
            selected,
            session_id=session.session_id,
            controllers=registry(
                activate if action == "release" else fail,
                fail if action == "release" else deactivate,
            ),
        )
    assert store.load_harness(session.session_id) == before
    events = store.load_session(session.session_id).events
    assert events[-1].payload["phase"] == "failed"


async def test_competing_controller_calls_cannot_overwrite_an_accepted_state(store):
    session = SessionRecord()
    store.create_session(session)
    count = 0
    ready = asyncio.Event()

    async def activate(call):
        nonlocal count
        count += 1
        call.session_state.replace(state_schema_version=1, payload=call.activation_id)
        if count == 2:
            ready.set()
        await ready.wait()
        return sdk.SessionControllerOutput()

    coordinator = SessionControlCoordinator(store)
    controllers = registry(activate, deactivate)
    results = await asyncio.gather(
        *(
            coordinator.apply(
                transition(),
                session_id=session.session_id,
                controllers=controllers,
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(item, SessionCommitConflict) for item in results) == 1
    state = store.load_harness(session.session_id).state
    assert state.states[0].payload == state.controls.activations[0].activation_id
