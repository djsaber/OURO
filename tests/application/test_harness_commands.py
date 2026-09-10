from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest

from ouro.application.plugin_commands import execute_plugin_command
from ouro.application.queued_inputs import QueuedInputQueue
from ouro.application.session_controls import SessionControlError
from ouro.application.sessions import SessionService
from ouro.core.errors import PersistenceError
from ouro.core.messages import MessageRecord
from ouro.core.observers import ObserverRegistry
from ouro.core.session_commits import SessionCommit, SessionCommitConflict, SessionCommitStep
from ouro.core.state import SessionRecord
from ouro.plugins.api import adapt_sdk_contributions
from ouro.plugins.catalog import ExtensionCatalogBase, build_extension_catalog
from ouro.plugins.registry import PluginCommandRegistry
from ouro.plugins.session_controllers import PluginSessionControllerRegistry
from ouro.sdk.plugins import v2 as sdk
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import ToolRegistry

OWNER = "example.command"


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    store = (
        LocalSessionStore(tmp_path / "files")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "db")
    )
    store.initialize()
    return store


def catalog(activate):
    def command(call):
        call.session_state.replace(state_schema_version=1, payload={"command": True})
        return sdk.CommandOutput(
            output="accepted",
            session_control_transitions=(sdk.SessionControlTransition("acquire", "mode"),),
            session_input_batches=(
                sdk.SessionInputBatch(
                    inputs=(sdk.SessionInput(prompt="first"), sdk.SessionInput(prompt="second"))
                ),
            ),
        )

    contributions = adapt_sdk_contributions(
        sdk.ContributionSet(
            commands=[sdk.CommandContribution(id="start", description="start", handler=command)],
            session_controllers=[
                sdk.SessionControllerContribution(
                    id="mode",
                    description="mode",
                    claims=(sdk.SessionControlClaim("example.mode", "exclusive"),),
                    activate=activate,
                    deactivate=lambda call: sdk.SessionControllerOutput(),
                )
            ],
        ),
        owner_id=OWNER,
    )
    commands = PluginCommandRegistry()
    commands.register(
        replace(
            contributions.commands[0],
            plugin_id=OWNER,
            generation_id="generation",
            execution_contract_hash="c" * 64,
            allow_session_inputs=True,
        )
    )
    controllers = PluginSessionControllerRegistry()
    controllers.replace_owner(
        "instance",
        (
            replace(
                contributions.session_controllers[0],
                plugin_id=OWNER,
                generation_id="generation",
                release_id="release",
                execution_contract_hash="c" * 64,
            ),
        ),
    )
    return build_extension_catalog(
        ExtensionCatalogBase.capture(
            tools=ToolRegistry(),
            observers=ObserverRegistry(),
            commands=commands,
            compaction=None,
            continuations=None,
            tool_policies=None,
            session_controllers=controllers,
        ),
        (),
    )


async def execute(store, session, activate):
    with catalog(activate).acquire() as lease:
        return await execute_plugin_command(
            catalog=lease,
            store=store,
            command_id=f"{OWNER}:start",
            argument="",
            session_id=session.session_id,
            workspace=None,
        )


async def test_controller_declares_native_usage_scope_in_the_accepted_control(store):
    def activate_scope(call):
        return sdk.SessionControllerOutput(usage_scope="goal-identity")

    session = SessionRecord()
    store.create_session(session)
    await execute(store, session, activate_scope)
    control = store.load_harness(session.session_id).state.controls.activations[0]
    assert control.plugin_id == OWNER
    assert control.usage_scope == "goal-identity"
    assert store.load_session_record(session.session_id).controls.activations[0] == control


@pytest.mark.parametrize("winner", ["command", "undo"])
async def test_command_and_undo_accept_only_the_winning_history(store, winner):
    session = SessionRecord()
    store.create_session(session)
    message = MessageRecord.text(
        session_id=session.session_id,
        role="user",
        text="change state",
    )
    message.metadata["session_input_source"] = "user"
    store.commit_session(SessionCommit(before=session, steps=(SessionCommitStep(message=message),)))
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_activate(call):
        entered.set()
        await release.wait()
        return activate(call)

    task = asyncio.create_task(execute(store, session, held_activate))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        if winner == "command":
            release.set()
            await task
        undone = SessionService(store).undo(session.session_id, message_id=message.message_id)
        release.set()
        if winner == "undo":
            with pytest.raises(SessionCommitConflict):
                await task
        assert undone.session.history_epoch == 1
        state = store.load_harness(session.session_id).state
        assert not state.states and not state.controls.activations
        assert not pending(store, session)
        assert not store.load_messages_tail(session.session_id)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def activate(call):
    assert call.session_state.read().payload == {"command": True}
    call.session_state.replace(state_schema_version=1, payload={"command": True, "mode": True})
    return sdk.SessionControllerOutput()


def pending(store, session):
    return QueuedInputQueue(store).eligible_session(store.load_session_record(session.session_id))


@pytest.mark.parametrize("fault", [None, "before", "after"])
async def test_command_state_control_queue_and_result_share_one_decision(store, monkeypatch, fault):
    session = SessionRecord()
    store.create_session(session)
    commit = store.commit_session
    requests = []

    def interrupted(request):
        requests.append(request)
        if fault == "before":
            raise PersistenceError("before decision")
        result = commit(request)
        if fault == "after":
            raise PersistenceError("reply lost after decision")
        return result

    monkeypatch.setattr(store, "commit_session", interrupted)
    if fault == "before":
        with pytest.raises(PersistenceError, match="before decision"):
            await execute(store, session, activate)
        assert not store.load_harness(session.session_id).state.states
        assert not store.load_session_record(session.session_id).controls.activations
        assert not pending(store, session)
        assert store.load_commit(session.session_id, requests[0].operation_id) is None
        return

    result = await execute(store, session, activate)
    assert len(requests) == 1
    persisted = store.load_commit(session.session_id, requests[0].operation_id)
    assert persisted.output["plugin_command"] == result.model_dump(mode="json")
    state = store.load_harness(session.session_id).state
    assert state.states[0].payload == {"command": True, "mode": True}
    assert len(state.controls.activations) == 1
    items = pending(store, session)
    assert [item.prompt for item in items] == ["first", "second"]
    assert items[0].batch_id == items[1].batch_id
    assert result.accepted_inputs[0].queue_id == items[0].queue_id
    assert result.session_inputs == result.session_input_batches == []


async def test_failed_nested_controller_discards_the_command_and_its_inputs(store):
    session = SessionRecord()
    store.create_session(session)

    def fail(call):
        activate(call)
        raise ValueError("controller failed")

    with pytest.raises(SessionControlError, match="controller failed"):
        await execute(store, session, fail)
    state = store.load_harness(session.session_id).state
    assert not state.states and not state.controls.activations
    assert not pending(store, session)


async def test_cancellation_after_decision_returns_the_accepted_command(store, monkeypatch):
    session = SessionRecord()
    store.create_session(session)
    decided, release = threading.Event(), threading.Event()
    commit = store.commit_session

    def held_reply(request):
        result = commit(request)
        decided.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(store, "commit_session", held_reply)
    task = asyncio.create_task(execute(store, session, activate))
    try:
        assert await asyncio.to_thread(decided.wait, 5)
        task.cancel()
    finally:
        release.set()
    result = await task
    assert result.output == "accepted"
    items = pending(store, session)
    assert result.accepted_inputs[0].queue_id == items[0].queue_id
    assert len(items) == 2
