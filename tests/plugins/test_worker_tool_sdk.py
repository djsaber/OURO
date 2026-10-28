from __future__ import annotations

import asyncio
import threading
from types import MappingProxyType

import pytest

from ouro.sdk.plugins import v5 as sdk
from ouro.sdk.plugins.worker_tools import invoke_worker_tool


def params(*, writable=True):
    return {
        "call": {
            "tool_call_id": "call",
            "session_id": "s",
            "run_id": "r",
            "tool_name": "Tool",
            "arguments": {},
        },
        "session_state": {
            "writable": writable,
            "value": {"state_schema_version": 1, "payload": {"count": 1}},
            "controls": [
                {
                    "controller_id": "flow",
                    "activation_id": "a",
                    "data": {"label": "kept"},
                    "usage_scope": "batch",
                }
            ],
            "run_controls": [
                {"controller_id": "flow", "activation_id": "a", "usage_scope": "batch"}
            ],
            "usage_tokens": {"batch": 7},
        },
    }


async def test_worker_tool_session_view_stages_changes_and_expires_after_result():
    retained = []

    def execute(call):
        state = call.session_state
        retained.append(state)
        value = state.read()
        value.payload["count"] = 100
        assert state.read().payload == {"count": 1}
        controls = state.controls()
        controls[0].data["label"] = "changed"
        assert state.controls()[0].data == {"label": "kept"}
        assert state.run_controls()[0].usage_scope == "batch"
        assert state.usage_tokens() == {"batch": 7}
        state.replace(state_schema_version=1, payload={"count": 2})
        assert state.read().payload == {"count": 2}
        return sdk.ToolOutput(
            content=(sdk.ToolTextContent(text="saved"),),
            structured=MappingProxyType({"count": 2}),
        )

    tool = sdk.ToolContribution("tool", "Tool", "Test", execute)
    result = await invoke_worker_tool(tool, params())
    assert result["session_state"]["payload"] == {"count": 2}
    assert result["content"][0]["text"] == "saved"
    assert result["structured"] == {"count": 2}
    with pytest.raises(RuntimeError, match="finished"):
        retained[0].read()


@pytest.mark.parametrize("action", ["replace", "delete"])
async def test_read_only_worker_tool_view_rejects_mutation(action):
    def execute(call):
        if action == "delete":
            call.session_state.delete()
        else:
            call.session_state.replace(state_schema_version=1, payload={})
        return "unreachable"

    with pytest.raises(PermissionError, match="read-only"):
        await invoke_worker_tool(
            sdk.ToolContribution("tool", "Tool", "Test", execute), params(writable=False)
        )


@pytest.mark.parametrize("kind", ["answer", "unattended"])
async def test_worker_tool_wait_and_resume_preserve_resolution_and_state_proposal(kind):
    def execute(call):
        return sdk.ToolInputRequest({"prompt": "Continue?"})

    def resume(call):
        assert call.request == {"prompt": "Continue?"}
        assert call.resolution.kind == kind
        call.session_state.delete()
        return "resolved"

    tool = sdk.ToolContribution("tool", "Tool", "Test", execute, resume_handler=resume)
    result = await invoke_worker_tool(tool, params())
    assert result == {"input_request": {"prompt": "Continue?"}}
    raw = params()
    raw["call"].update(
        request=result["input_request"],
        resolution={"kind": "answer", "value": True}
        if kind == "answer"
        else {"kind": "unattended", "resolver_id": "policy", "reason": "background"},
    )
    assert await invoke_worker_tool(tool, raw, resume=True) == {
        "content": "resolved",
        "session_state": None,
    }


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_cancelled_worker_tool_returns_observation_and_discards_late_state(
    monkeypatch, asynchronous
):
    from ouro.sdk.plugins.worker_tools import WorkerSessionState

    entered, release = threading.Event(), threading.Event()
    release_async = asyncio.Event()
    closed = asyncio.Event()
    retained = []
    close = WorkerSessionState.close

    def observe_close(state):
        close(state)
        closed.set()

    monkeypatch.setattr(WorkerSessionState, "close", observe_close)

    def begin(call):
        retained.append(call.session_state)
        call.session_state.replace(state_schema_version=1, payload={"staged": True})
        entered.set()

    def finish(call):
        with pytest.raises(RuntimeError, match="finished"):
            call.session_state.replace(state_schema_version=1, payload={"late": True})
        return "late"

    def execute_sync(call):
        begin(call)
        release.wait()
        return finish(call)

    async def execute_async(call):
        begin(call)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release_async.wait()
            asyncio.current_task().uncancel()
        return finish(call)

    execute = execute_async if asynchronous else execute_sync
    task = asyncio.create_task(
        invoke_worker_tool(sdk.ToolContribution("tool", "Tool", "Test", execute), params())
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.wait_for(closed.wait(), 5)
        assert not task.done()
        release.set()
        release_async.set()
        assert await task == {"content": "late"}
        with pytest.raises(RuntimeError, match="finished"):
            retained[0].read()
    finally:
        release.set()
        release_async.set()
        await asyncio.gather(task, return_exceptions=True)
