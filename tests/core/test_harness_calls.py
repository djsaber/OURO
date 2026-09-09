from __future__ import annotations

import asyncio

import pytest

from ouro.core.cancellation import AbortSignal
from ouro.core.harness import HarnessState
from ouro.core.harness_calls import HarnessCallSnapshot, HarnessInvocation, accept_state_changes
from ouro.core.session_commits import SessionCommit, SessionCommitConflict, SessionCommitStep
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.core.state import SessionRecord
from ouro.sessions import LocalSessionStore, SQLiteSessionStore


def owner_value(value):
    return SessionExtensionStateRecord(
        session_id="ses_call",
        owner_id="example.plugin",
        state_schema_version=1,
        payload={"items": [value]},
    )


@pytest.fixture
def snapshot():
    return HarnessCallSnapshot(
        session_id="ses_call",
        harness_version="harness_before",
        history_epoch=2,
        state=HarnessState(states=(owner_value("before"),)),
    )


def test_owner_view_returns_copies_and_only_stages_replacements(snapshot):
    call = HarnessInvocation(snapshot, "example.plugin", writable=True)
    loaded = call.read()
    loaded.payload["items"].append("mutated copy")
    assert call.read().payload == {"items": ["before"]}
    replacement = owner_value("after")
    call.replace(replacement)
    replacement.payload["items"].append("mutated input")
    assert call.read().payload == {"items": ["after"]}
    proposal = call.finish()
    assert proposal is not None
    assert snapshot.state.states[0].payload == {"items": ["before"]}
    assert accept_state_changes(snapshot, [proposal]).state.states[0].payload == {
        "items": ["after"]
    }
    with pytest.raises(RuntimeError, match="closed"):
        call.delete()


@pytest.mark.parametrize(
    "write", [lambda call: call.replace(owner_value("bad")), lambda call: call.delete()]
)
def test_readonly_calls_reject_all_writes(snapshot, write):
    call = HarnessInvocation(snapshot, "example.plugin", writable=False)
    with pytest.raises(PermissionError, match="read-only"):
        write(call)
    assert call.finish() is None


@pytest.mark.parametrize("identity", [{"session_id": "ses_foreign"}, {"owner_id": "other.plugin"}])
def test_mutable_calls_cannot_change_their_owner_or_session(snapshot, identity):
    call = HarnessInvocation(snapshot, "example.plugin", writable=True)
    with pytest.raises(ValueError, match="another Session or owner"):
        call.replace(owner_value("bad").model_copy(update=identity))
    assert call.finish() is None


def test_replacing_the_same_business_value_proposes_no_change(snapshot):
    call = HarnessInvocation(snapshot, "example.plugin", writable=True)
    call.replace(owner_value("before"))
    assert call.finish() is None


def test_nested_callback_sees_the_accepted_parent_draft(snapshot):
    parent = HarnessInvocation(snapshot, "example.plugin", writable=True)
    parent.replace(owner_value("command"))
    command = parent.finish()
    draft = accept_state_changes(snapshot, [command])
    controller = HarnessInvocation(draft, "example.plugin", writable=True)
    assert controller.read().payload == {"items": ["command"]}
    controller.delete()
    assert accept_state_changes(draft, [controller.finish()]).state.states == ()
    assert snapshot.state.states[0].payload == {"items": ["before"]}


def test_cancelled_call_cannot_produce_a_proposal(snapshot):
    signal = AbortSignal()
    call = HarnessInvocation(snapshot, "example.plugin", writable=True, signal=signal)
    call.replace(owner_value("discarded"))
    signal.abort("test cancellation")
    with pytest.raises(RuntimeError, match="cancelled"):
        call.finish()
    with pytest.raises(RuntimeError, match="closed"):
        call.read()


@pytest.mark.asyncio
async def test_saved_background_view_cannot_write_after_callback_returns(snapshot):
    call = HarnessInvocation(snapshot, "example.plugin", writable=True)
    returned = asyncio.Event()

    async def background():
        await returned.wait()
        with pytest.raises(RuntimeError, match="closed"):
            call.replace(owner_value("late"))

    task = asyncio.create_task(background())
    assert call.finish() is None
    returned.set()
    await task


def test_failed_aggregation_keeps_all_proposals_unaccepted(snapshot):
    first = HarnessInvocation(snapshot, "example.plugin", writable=True)
    second = HarnessInvocation(snapshot, "example.plugin", writable=True)
    first.replace(owner_value("first"))
    second.replace(owner_value("second"))
    with pytest.raises(ValueError, match="conflicting Harness proposals"):
        accept_state_changes(snapshot, [first.finish(), second.finish()])
    assert snapshot.state.states[0].payload == {"items": ["before"]}


@pytest.mark.parametrize("backend", [LocalSessionStore, SQLiteSessionStore])
def test_application_accepts_a_proposal_only_with_its_original_store_preconditions(
    tmp_path, backend
):
    store = backend(tmp_path / "store")
    store.initialize()
    session = SessionRecord(session_id="ses_call")
    store.create_session(session)
    snapshot = HarnessCallSnapshot(
        session_id=session.session_id,
        harness_version=session.harness_version,
        history_epoch=session.history_epoch,
        state=store.load_harness(session.session_id).state,
    )
    call = HarnessInvocation(snapshot, "example.plugin", writable=True)
    call.replace(owner_value("result"))
    proposal = call.finish()
    assert store.load_session_extension_states(session.session_id) == []
    draft = accept_state_changes(snapshot, [proposal])
    store.save_session_extension_state(owner_value("concurrent"))
    with pytest.raises(SessionCommitConflict):
        store.commit_session(
            SessionCommit(before=session, steps=(SessionCommitStep(harness=draft.state),))
        )
    assert store.load_session_extension_states(session.session_id)[0].payload == {
        "items": ["concurrent"]
    }
