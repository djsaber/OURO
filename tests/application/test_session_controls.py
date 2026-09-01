from __future__ import annotations

from threading import get_ident

import pytest

from ouro.application.session_controls import (
    SessionControlConflictError,
    SessionControlCoordinator,
    SessionControlError,
)
from ouro.core.session_controls import (
    AttributedSessionControlTransition,
    SessionControlClaim,
)
from ouro.core.state import SessionRecord
from ouro.plugins.session_controllers import (
    PluginSessionController,
    PluginSessionControllerCall,
    PluginSessionControllerOutput,
    PluginSessionControllerRegistry,
)
from ouro.sessions import LocalSessionStore, SQLiteSessionStore

_CONTRACT = "c" * 64


def _store(tmp_path, backend: str):
    store = (
        LocalSessionStore(tmp_path / "files")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    )
    store.initialize()
    return store


def _controller(
    plugin_id: str,
    controller_id: str,
    domain: str,
    events: list[str],
    *,
    fail_activate: bool = False,
) -> PluginSessionController:
    async def activate(call):
        events.append(f"activate:{call.controller_id}:{call.activation_id}")
        if fail_activate:
            raise RuntimeError("activation failed")
        return PluginSessionControllerOutput(
            output=f"activated {call.controller_id}",
            data={"activation_id": call.activation_id},
            owner_state_revision=1,
        )

    async def deactivate(call):
        events.append(f"deactivate:{call.controller_id}:{call.activation_id}")
        return PluginSessionControllerOutput(output=f"deactivated {call.controller_id}")

    return PluginSessionController(
        controller_id=controller_id,
        description=f"Controller {controller_id}",
        claims=(SessionControlClaim(domain=domain, access="exclusive"),),
        activate_handler=activate,
        deactivate_handler=deactivate,
        plugin_id=plugin_id,
        plugin_class="user",
        release_id=f"release-{controller_id}",
        package_digest=f"release-{controller_id}",
        generation_id=f"generation-{controller_id}",
        contract_hash="d" * 64,
        execution_contract_hash=_CONTRACT,
    )


def _transition(
    plugin_id: str,
    generation_id: str,
    controller_id: str,
    action: str,
) -> AttributedSessionControlTransition:
    return AttributedSessionControlTransition(
        action=action,
        controller_id=controller_id,
        plugin_id=plugin_id,
        generation_id=generation_id,
        execution_contract_hash=_CONTRACT,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_session_control_persistence_phases_run_outside_the_event_loop(
    tmp_path,
    backend,
    monkeypatch,
) -> None:
    store = _store(tmp_path, backend)
    session = SessionRecord()
    store.create_session(session)
    events: list[str] = []
    controller = _controller(
        "dev.example.thread-boundary",
        "main",
        "dev.example.thread-boundary",
        events,
    )
    registry = PluginSessionControllerRegistry()
    registry.replace_owner("thread-boundary", (controller,))
    coordinator = SessionControlCoordinator(store)
    event_loop_thread = get_ident()
    reader_threads: list[int] = []
    load_session = store.load_session_record

    def recording_load_session(session_id: str):
        reader_threads.append(get_ident())
        return load_session(session_id)

    monkeypatch.setattr(store, "load_session_record", recording_load_session)

    acquired = await coordinator.apply(
        _transition(
            "dev.example.thread-boundary",
            "generation-main",
            "main",
            "acquire",
        ),
        session_id=session.session_id,
        controllers=registry,
    )

    assert acquired.changed is True
    assert len(reader_threads) >= 2
    assert event_loop_thread not in reader_threads


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_session_control_claims_arbitrate_unknown_plugins_without_business_rules(
    tmp_path,
    backend,
) -> None:
    store = _store(tmp_path, backend)
    session = SessionRecord()
    store.create_session(session)
    events: list[str] = []
    first = _controller(
        "dev.example.first",
        "main",
        "dev.example.shared-domain",
        events,
    )
    second = _controller(
        "dev.example.second",
        "main",
        "dev.example.shared-domain",
        events,
    )
    independent = _controller(
        "dev.example.third",
        "main",
        "dev.example.independent-domain",
        events,
    )
    registry = PluginSessionControllerRegistry()
    registry.replace_owner("first", (first,))
    registry.replace_owner("second", (second,))
    registry.replace_owner("third", (independent,))
    registry.freeze()
    coordinator = SessionControlCoordinator(store)

    acquired = await coordinator.apply(
        _transition("dev.example.first", "generation-main", "main", "acquire"),
        session_id=session.session_id,
        controllers=registry,
    )
    replayed = await coordinator.apply(
        _transition("dev.example.first", "generation-main", "main", "acquire"),
        session_id=session.session_id,
        controllers=registry,
    )
    assert acquired.changed is True
    assert replayed.changed is False

    with pytest.raises(SessionControlConflictError, match="shared-domain"):
        await coordinator.apply(
            _transition("dev.example.second", "generation-main", "main", "acquire"),
            session_id=session.session_id,
            controllers=registry,
        )

    third = await coordinator.apply(
        _transition("dev.example.third", "generation-main", "main", "acquire"),
        session_id=session.session_id,
        controllers=registry,
    )
    snapshot = store.load_runtime_session(session.session_id)
    assert third.changed is True
    assert {item.plugin_id for item in snapshot.session.controls.activations} == {
        "dev.example.first",
        "dev.example.third",
    }
    assert not any("dev.example.second" in event for event in events)

    released = await coordinator.apply(
        _transition("dev.example.first", "generation-main", "main", "release"),
        session_id=session.session_id,
        controllers=registry,
    )
    replayed_release = await coordinator.apply(
        _transition("dev.example.first", "generation-main", "main", "release"),
        session_id=session.session_id,
        controllers=registry,
    )
    assert released.changed is True
    assert replayed_release.changed is False
    remaining = store.load_runtime_session(session.session_id).session.controls.activations
    assert remaining[0].plugin_id == "dev.example.third"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_failed_controller_activation_rolls_back_host_state(tmp_path, backend) -> None:
    store = _store(tmp_path, backend)
    session = SessionRecord()
    store.create_session(session)
    events: list[str] = []
    controller = _controller(
        "dev.example.failure",
        "main",
        "dev.example.failure-domain",
        events,
        fail_activate=True,
    )
    registry = PluginSessionControllerRegistry()
    registry.replace_owner("failure", (controller,))
    coordinator = SessionControlCoordinator(store)

    with pytest.raises(SessionControlError, match="activation failed"):
        await coordinator.apply(
            _transition("dev.example.failure", "generation-main", "main", "acquire"),
            session_id=session.session_id,
            controllers=registry,
        )

    controls = store.load_runtime_session(session.session_id).session.controls
    assert controls.activations == []
    assert controls.revision == 2


@pytest.mark.asyncio
async def test_controller_lifecycle_receives_session_kind_and_symmetric_release_payload(
    tmp_path,
) -> None:
    store = _store(tmp_path, "files")
    session = SessionRecord(
        root_session_id="ses_root",
        parent_session_id="ses_parent",
        session_kind="subagent",
        delegation_id="delegation_1",
    )
    store.create_session(session)
    calls: list[PluginSessionControllerCall] = []

    async def lifecycle(call: PluginSessionControllerCall) -> PluginSessionControllerOutput:
        calls.append(call)
        return PluginSessionControllerOutput()

    controller = PluginSessionController(
        controller_id="mode",
        description="Own a generic mode.",
        claims=(SessionControlClaim(domain="dev.example.lifecycle"),),
        activate_handler=lifecycle,
        deactivate_handler=lifecycle,
        plugin_id="dev.example.lifecycle",
        plugin_class="user",
        release_id="release-lifecycle",
        package_digest="release-lifecycle",
        generation_id="generation-lifecycle",
        contract_hash="d" * 64,
        execution_contract_hash=_CONTRACT,
    )
    registry = PluginSessionControllerRegistry()
    registry.replace_owner("lifecycle", (controller,))
    coordinator = SessionControlCoordinator(store)

    acquired = await coordinator.apply(
        AttributedSessionControlTransition(
            action="acquire",
            controller_id="mode",
            payload={"command": "start"},
            plugin_id="dev.example.lifecycle",
            generation_id="generation-lifecycle",
            execution_contract_hash=_CONTRACT,
        ),
        session_id=session.session_id,
        controllers=registry,
    )
    assert acquired.activation is not None
    released = await coordinator.apply(
        AttributedSessionControlTransition(
            action="release",
            activation_id=acquired.activation.activation_id,
            payload={"status": "complete", "report": "verified"},
            plugin_id="dev.example.lifecycle",
            generation_id="generation-lifecycle",
            execution_contract_hash=_CONTRACT,
        ),
        session_id=session.session_id,
        controllers=registry,
    )

    assert released.changed is True
    assert [call.session_kind for call in calls] == ["subagent", "subagent"]
    assert calls[0].payload == {"command": "start"}
    assert calls[1].payload == {"status": "complete", "report": "verified"}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_replace_is_an_explicit_same_plugin_handoff(tmp_path, backend) -> None:
    store = _store(tmp_path, backend)
    session = SessionRecord()
    store.create_session(session)
    events: list[str] = []
    primary = _controller(
        "dev.example.modes",
        "primary",
        "dev.example.mode",
        events,
    )
    alternate = _controller(
        "dev.example.modes",
        "alternate",
        "dev.example.mode",
        events,
    )
    registry = PluginSessionControllerRegistry()
    registry.replace_owner("modes", (primary, alternate))
    coordinator = SessionControlCoordinator(store)

    acquired = await coordinator.apply(
        _transition(
            "dev.example.modes",
            "generation-primary",
            "primary",
            "acquire",
        ),
        session_id=session.session_id,
        controllers=registry,
    )
    assert acquired.activation is not None

    replaced = await coordinator.apply(
        AttributedSessionControlTransition(
            action="replace",
            controller_id="alternate",
            replace_activation_ids=[acquired.activation.activation_id],
            plugin_id="dev.example.modes",
            generation_id="generation-alternate",
            execution_contract_hash=_CONTRACT,
        ),
        session_id=session.session_id,
        controllers=registry,
    )

    assert replaced.activation is not None
    assert replaced.activation.controller_local_id == "alternate"
    assert replaced.released_activation_ids == [acquired.activation.activation_id]
    assert "/session_controllers/alternate:" in events[1]
    assert events[1].startswith("activate:")
    assert "/session_controllers/primary:" in events[2]
    assert events[2].startswith("deactivate:")
    controls = store.load_session_record(session.session_id).controls
    assert [item.controller_local_id for item in controls.activations] == ["alternate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_replace_can_rotate_the_same_controller_activation(tmp_path, backend) -> None:
    store = _store(tmp_path, backend)
    session = SessionRecord()
    store.create_session(session)
    calls = []

    async def lifecycle(call):
        calls.append(call)
        return PluginSessionControllerOutput()

    controller = PluginSessionController(
        controller_id="mode",
        description="Rotate one controller identity.",
        claims=(SessionControlClaim(domain="dev.example.rotating-mode"),),
        activate_handler=lifecycle,
        deactivate_handler=lifecycle,
        plugin_id="dev.example.rotating",
        plugin_class="user",
        release_id="release-rotating",
        package_digest="release-rotating",
        generation_id="generation-rotating",
        contract_hash="d" * 64,
        execution_contract_hash=_CONTRACT,
    )
    registry = PluginSessionControllerRegistry()
    registry.replace_owner("rotating", (controller,))
    coordinator = SessionControlCoordinator(store)
    acquired = await coordinator.apply(
        AttributedSessionControlTransition(
            action="acquire",
            controller_id="mode",
            plugin_id="dev.example.rotating",
            generation_id="generation-rotating",
            execution_contract_hash=_CONTRACT,
        ),
        session_id=session.session_id,
        controllers=registry,
    )
    assert acquired.activation is not None

    replaced = await coordinator.apply(
        AttributedSessionControlTransition(
            action="replace",
            controller_id="mode",
            replace_activation_ids=[acquired.activation.activation_id],
            payload={"operation": "rotate"},
            plugin_id="dev.example.rotating",
            generation_id="generation-rotating",
            execution_contract_hash=_CONTRACT,
        ),
        session_id=session.session_id,
        controllers=registry,
    )

    assert replaced.activation is not None
    assert replaced.activation.activation_id != acquired.activation.activation_id
    assert replaced.released_activation_ids == [acquired.activation.activation_id]
    assert [call.action for call in calls] == ["activate", "activate", "deactivate"]
    assert calls[1].payload == {"operation": "rotate"}
    controls = store.load_session_record(session.session_id).controls
    assert [item.activation_id for item in controls.activations] == [
        replaced.activation.activation_id
    ]


@pytest.mark.asyncio
async def test_replace_cannot_take_over_another_plugin_activation(tmp_path) -> None:
    store = _store(tmp_path, "files")
    session = SessionRecord()
    store.create_session(session)
    events: list[str] = []
    owner = _controller(
        "dev.example.owner",
        "main",
        "dev.example.mode",
        events,
    )
    intruder = _controller(
        "dev.example.intruder",
        "main",
        "dev.example.mode",
        events,
    )
    registry = PluginSessionControllerRegistry()
    registry.replace_owner("controllers", (owner, intruder))
    coordinator = SessionControlCoordinator(store)
    acquired = await coordinator.apply(
        _transition("dev.example.owner", "generation-main", "main", "acquire"),
        session_id=session.session_id,
        controllers=registry,
    )
    assert acquired.activation is not None

    with pytest.raises(SessionControlConflictError, match="cannot replace another plugin"):
        await coordinator.apply(
            AttributedSessionControlTransition(
                action="replace",
                controller_id="main",
                replace_activation_ids=[acquired.activation.activation_id],
                plugin_id="dev.example.intruder",
                generation_id="generation-main",
                execution_contract_hash=_CONTRACT,
            ),
            session_id=session.session_id,
            controllers=registry,
        )


@pytest.mark.parametrize("backend", ["files", "sqlite"])
def test_interrupted_lifecycle_reconciliation_fails_closed(tmp_path, backend) -> None:
    store = _store(tmp_path, backend)
    session = SessionRecord()
    store.create_session(session)
    activating = _controller(
        "dev.example.interrupted",
        "starting",
        "dev.example.starting",
        [],
    )
    deactivating = _controller(
        "dev.example.interrupted",
        "stopping",
        "dev.example.stopping",
        [],
    )
    controls = session.controls.model_copy(
        update={
            "revision": 1,
            "activations": [
                SessionControlCoordinator._new_activation(activating),
                SessionControlCoordinator._new_activation(deactivating).model_copy(
                    update={"phase": "deactivating"}
                ),
            ],
        }
    )
    store.save_session(session.model_copy(update={"controls": controls}))
    coordinator = SessionControlCoordinator(store)

    assert coordinator.reconcile_interrupted(session.session_id) is True

    reconciled = store.load_session_record(session.session_id).controls
    assert reconciled.revision == 2
    assert len(reconciled.activations) == 1
    assert reconciled.activations[0].controller_local_id == "starting"
    assert reconciled.activations[0].phase == "failed"
    assert "interrupted" in (reconciled.activations[0].error or "")
