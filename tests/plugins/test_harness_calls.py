from __future__ import annotations

import asyncio
import threading

import pytest
from pydantic import ValidationError

from ouro.core.callbacks import CallbackOutcomeUnknown, invoke_callback
from ouro.core.harness import HarnessState
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.core.session_controls import SessionControlActivation, SessionControlState
from ouro.core.session_extensions import SessionExtensionStateRecord
from ouro.plugins.harness_calls import PluginStateView, invoke_plugin_callback
from ouro.sdk.plugins import v2 as sdk


@pytest.fixture
def snapshot():
    return HarnessCallSnapshot(
        session_id="ses_plugin",
        harness_version="harness_before",
        history_epoch=1,
        state=HarnessState(
            states=(
                SessionExtensionStateRecord(
                    session_id="ses_plugin",
                    owner_id="example.plugin",
                    state_schema_version=1,
                    payload={"items": ["before"]},
                ),
                SessionExtensionStateRecord(
                    session_id="ses_plugin",
                    owner_id="other.plugin",
                    state_schema_version=1,
                    payload="private",
                ),
            )
        ),
    )


def test_sdk_state_view_binds_identity_and_validates_payload_at_the_boundary(snapshot):
    view = PluginStateView(snapshot, "example.plugin", writable=True)
    assert view.read().payload == {"items": ["before"]}
    with pytest.raises(ValidationError):
        view.replace(state_schema_version=0, payload={"items": []})
    view.replace(state_schema_version=1, payload={"items": ["after"]})
    proposal = view.finish()
    assert proposal.owner_id == "example.plugin"
    assert proposal.session_id == "ses_plugin"
    assert proposal.harness_version == "harness_before"
    assert proposal.history_epoch == 1
    assert proposal.state.payload == {"items": ["after"]}
    assert snapshot.state.states[1].payload == "private"


def test_controller_projection_is_owner_scoped_detached_and_call_bound(snapshot):
    controls = SessionControlState(
        activations=[
            SessionControlActivation(
                controller_id=f"{owner}/session_controllers/mode",
                controller_local_id="mode",
                plugin_id=owner,
                plugin_class="user",
                release_id="release",
                generation_id="generation",
                execution_contract_hash="c" * 64,
                data={"values": ["original"]},
            )
            for owner in ("example.plugin", "other.plugin")
        ]
    )
    snapshot = snapshot.model_copy(
        update={"state": snapshot.state.model_copy(update={"controls": controls})}
    )
    view = PluginStateView(snapshot, "example.plugin", writable=False)
    projected = view.controls()
    assert len(projected) == 1 and projected[0].controller_id == "mode"
    projected[0].data["values"].append("local")
    assert view.controls()[0].data == {"values": ["original"]}
    assert view.finish() is None
    with pytest.raises(RuntimeError, match="closed"):
        view.controls()


@pytest.mark.asyncio
async def test_a_timed_out_synchronous_plugin_cannot_reuse_its_state_view(snapshot):
    view = PluginStateView(snapshot, "example.plugin", writable=True)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    failures: list[str] = []

    def handler():
        entered.set()
        try:
            assert release.wait(5)
            view.replace(state_schema_version=1, payload="late")
        except RuntimeError as exc:
            failures.append(str(exc))
        finally:
            finished.set()

    task = asyncio.create_task(invoke_callback(handler))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        view.close()
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    assert failures == ["Harness invocation is closed"]


@pytest.mark.parametrize("operation", ["replace", "delete"])
def test_sdk_readonly_state_view_cannot_stage_changes(snapshot, operation):
    view = PluginStateView(snapshot, "example.plugin", writable=False)
    with pytest.raises(PermissionError, match="read-only"):
        if operation == "replace":
            view.replace(state_schema_version=1, payload="not accepted")
        else:
            view.delete()
    assert view.finish() is None


@pytest.mark.asyncio
async def test_owned_sync_callback_loses_state_access_before_its_thread_settles(snapshot):
    entered = threading.Event()
    release = threading.Event()
    failures = []
    views = []

    def handler(call):
        views.append(call.session_state)
        entered.set()
        assert release.wait(5)
        try:
            call.session_state.replace(state_schema_version=1, payload="late")
        except RuntimeError as exc:
            failures.append(str(exc))
        return sdk.ToolOutput(content="external callback finished")

    task = asyncio.create_task(
        invoke_plugin_callback(
            handler,
            sdk.ToolCall("call", "ses_plugin", "run", "State", {}),
            snapshot=snapshot,
            owner_id="example.plugin",
            writable=True,
            owned=True,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(RuntimeError, match="closed"):
            views[0].read()
    finally:
        release.set()
        with pytest.raises(CallbackOutcomeUnknown):
            await task
    assert failures == ["Harness invocation is closed"]


@pytest.mark.asyncio
async def test_background_task_cannot_reuse_a_successfully_returned_callback_view(snapshot):
    release = asyncio.Event()
    background = []

    async def handler(call):
        async def late_write():
            await release.wait()
            call.session_state.delete()

        background.append(asyncio.create_task(late_write()))
        call.session_state.replace(state_schema_version=1, payload="accepted proposal")
        return sdk.ToolOutput(content="done")

    _, change = await invoke_plugin_callback(
        handler,
        sdk.ToolCall("call", "ses_plugin", "run", "State", {}),
        snapshot=snapshot,
        owner_id="example.plugin",
        writable=True,
    )
    release.set()
    with pytest.raises(RuntimeError, match="closed"):
        await background[0]
    assert change.state.payload == "accepted proposal"
