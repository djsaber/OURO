from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ouro.core.harness import HarnessState
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.plugins.harness_calls import invoke_plugin_callback
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.worker import WorkerPluginInstance
from ouro.sdk.plugins import v4 as sdk


@pytest.fixture
async def worker(tmp_path):
    package = Path(__file__).parents[2] / "examples/plugins/worker-tool"
    manifest = PluginManifest.model_validate_json(
        (package / ".ouro-plugin/plugin.json").read_text(encoding="utf-8")
    )
    (tmp_path / "worker.py").write_text(
        """import json, sys
held = None
def respond(identity, result):
    print(json.dumps({'jsonrpc': '2.0', 'id': identity, 'result': result}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    method = request['method']
    result = {}
    if method == 'initialize':
        result = {'protocol_version': '3.0'}
    elif method == 'contributions.prepare':
        result = {'tools': {'echo': {}}}
    elif method == 'tool.call':
        call = request['params']['call']
        result = {'content': call['session_id'], 'session_state': {
            'state_schema_version': 1, 'payload': call['session_id']}}
        if call['arguments'].get('hold'):
            held = (request['id'], result)
            continue
    elif method == 'ready':
        result = {'held': held is not None}
    elif method == 'cancel':
        if held is not None:
            respond(*held)
            held = None
        continue
    if isinstance(request.get('id'), int):
        respond(request['id'], result)
    if method == 'shutdown':
        break
""",
        encoding="utf-8",
    )
    instance = WorkerPluginInstance(
        manifest,
        package_root=tmp_path,
        package_digest="test",
        permissions=frozenset(),
        configuration={},
        data_directory=tmp_path / "data",
        credentials={},
        diagnostic=lambda *_args: None,
    )
    try:
        contributions = await instance.prepare()
        yield instance, contributions.tools[0].handler, manifest.plugin_id
    finally:
        await instance.stop("shutdown", 0)


def invocation(handler, owner, session_id, *, writable=True, hold=False):
    return invoke_plugin_callback(
        handler,
        sdk.ToolCall(
            tool_call_id="call",
            session_id=session_id,
            run_id="run",
            tool_name="echo",
            arguments={"hold": hold},
        ),
        snapshot=HarnessCallSnapshot(
            session_id=session_id,
            harness_version="before",
            history_epoch=1,
            state=HarnessState(),
        ),
        owner_id=owner,
        writable=writable,
    )


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_late_worker_state_cannot_escape_closed_call_or_enter_another_session(worker, stop):
    instance, handler, owner = worker
    views = []

    async def capture(call):
        views.append(call.session_state)
        return await handler(call)

    pending = asyncio.create_task(invocation(capture, owner, "withdrawn", hold=True))
    try:
        async with asyncio.timeout(5):
            while not (await instance.client.request("ready"))["held"]:
                pass
        if stop == "cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(pending, 0.01)
        with pytest.raises(RuntimeError, match="closed"):
            views[0].read()

        # Worker replies to the cancelled request before answering this one.
        # Receiving this result proves the late reply crossed the reader boundary.
        output, change = await invocation(handler, owner, "other-session")
        assert output.content == "other-session"
        assert change.session_id == "other-session"
        assert change.owner_id == owner
        assert change.state.payload == "other-session"
        assert instance.client.is_healthy
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_worker_cannot_propose_state_from_a_readonly_call(worker):
    instance, handler, owner = worker
    with pytest.raises(PermissionError, match="read-only"):
        await invocation(handler, owner, "readonly", writable=False)
    output, change = await invocation(handler, owner, "writable")
    assert output.content == "writable"
    assert change.state.payload == "writable"
    assert instance.client.is_healthy
