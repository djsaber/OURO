"""Handshake-controlled external effects for late worker result integration tests."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from ouro.sdk.plugins import v5 as sdk
from ouro.sdk.plugins.worker import HostRequestError, WorkerPeer
from ouro.sdk.plugins.worker_tools import WorkerSessionState, invoke_worker_tool


async def main() -> None:
    loop = asyncio.get_running_loop()
    released = threading.Event()
    released_async = asyncio.Event()
    closed = asyncio.Event()
    original_close = WorkerSessionState.close

    def observe_close(state):
        original_close(state)
        closed.set()

    WorkerSessionState.close = observe_close

    async def begin(call):
        with Path(call.arguments["effect_file"]).open("a", encoding="utf-8") as stream:
            stream.write("performed\n")
        call.session_state.replace(state_schema_version=1, payload={"staged": True})
        await peer.notify("diagnostic.emit", {"code": "entered", "message": "effect performed"})

    async def finish(call):
        try:
            call.session_state.replace(state_schema_version=1, payload={"late": True})
        except RuntimeError:
            pass
        else:
            raise AssertionError("cancelled worker state remained writable")
        try:
            await peer.request("forbidden.late", {})
        except HostRequestError as exc:
            assert exc.code == -32602
        else:
            raise AssertionError("cancelled request retained Host authority")
        return sdk.ToolOutput(
            content="worker actual late result",
            structured={"external_effect": "performed", "reverse_call_rejected": True},
            session_control_transitions=[
                sdk.SessionControlTransition(action="acquire", controller_id="unavailable")
            ],
        )

    def execute_sync(call):
        asyncio.run_coroutine_threadsafe(begin(call), loop).result()
        assert released.wait(60)
        return asyncio.run_coroutine_threadsafe(finish(call), loop).result()

    async def execute_async(call):
        await begin(call)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await released_async.wait()
        return await finish(call)

    async def initialize(params):
        return {"protocol_version": "3.0"}

    async def prepare(params):
        return {"tools": {"echo": {}}}

    async def invoke(params):
        handler = execute_async if params["call"]["arguments"]["asynchronous"] else execute_sync
        return await invoke_worker_tool(
            sdk.ToolContribution("echo", "Echo", "Late", handler), params
        )

    async def wait_closed(params):
        await closed.wait()
        return True

    async def release(params):
        released.set()
        released_async.set()
        return True

    peer = WorkerPeer(
        {
            "initialize": initialize,
            "contributions.prepare": prepare,
            "tool.call": invoke,
            "test.closed": wait_closed,
            "test.release": release,
        }
    )
    await peer.serve()


asyncio.run(main())
