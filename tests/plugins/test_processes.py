from __future__ import annotations

import asyncio
import sys

import pytest

import ouro.local_processes.commands as local_commands
from ouro.local_locks import is_lock_held
from ouro.plugins.catalog.gates import GenerationGate, PluginGenerationUnavailableError
from ouro.plugins.invocation import plugin_invocation
from ouro.plugins.processes import PluginProcessService
from ouro.sdk.plugins.processes import ProcessRequest
from tests.concurrency import wait_until


async def test_plugin_process_output_is_ordered_and_final_capture_is_bounded():
    port = PluginProcessService(GenerationGate("process-test"), frozenset({"process.spawn"}))
    chunks = []

    async def collect(chunk):
        chunks.append(chunk)

    result = await port.run(
        ProcessRequest((sys.executable, "-c", "import sys;sys.stdout.write('x'*300000)")),
        on_output=collect,
    )
    assert result.returncode == 0
    assert result.stdout_truncated
    assert len(result.stdout) <= 100_000
    assert "".join(chunk.text for chunk in chunks) == "x" * 300_000
    assert [chunk.sequence for chunk in chunks] == list(range(1, len(chunks) + 1))


async def test_process_permission_and_read_only_calls_reject_execution():
    request = ProcessRequest((sys.executable, "-c", "raise AssertionError('must not run')"))
    gate = GenerationGate("process-test")
    with pytest.raises(PermissionError, match="process.spawn"):
        await PluginProcessService(gate, frozenset()).run(request)
    with plugin_invocation("plugin", writable=False):
        with pytest.raises(PermissionError, match="read-only"):
            await PluginProcessService(gate, frozenset({"process.spawn"})).run(request)
    gate.close()
    with pytest.raises(PluginGenerationUnavailableError):
        await PluginProcessService(gate, frozenset({"process.spawn"})).run(request)


async def test_nested_process_call_preserves_workspace_restriction(tmp_path):
    from ouro.tools.base import ToolPolicyError

    port = PluginProcessService(GenerationGate("process-test"), frozenset({"process.spawn"}))
    with plugin_invocation("caller", writable=True, workspace=str(tmp_path), workspace_only=True):
        with plugin_invocation("service", writable=True, workspace_only=False):
            with pytest.raises(ToolPolicyError, match="outside"):
                await port.run(
                    ProcessRequest(
                        (sys.executable, "-c", "print('forbidden')"), cwd=str(tmp_path.parent)
                    )
                )


async def test_generation_revocation_stops_streaming_process_before_returning(monkeypatch):
    gate = GenerationGate("process-test")
    port = PluginProcessService(gate, frozenset({"process.spawn"}))
    ready = asyncio.Event()
    owners = []
    start_process = local_commands.start_process

    async def start(*args, **kwargs):
        owner = await start_process(*args, **kwargs)
        owners.append(owner)
        return owner

    monkeypatch.setattr(local_commands, "start_process", start)

    async def output(chunk):
        ready.set()

    code = "import threading;print('ready',flush=True);threading.Event().wait()"
    running = asyncio.create_task(
        port.run(ProcessRequest((sys.executable, "-c", code)), on_output=output)
    )
    try:
        async with asyncio.timeout(30):
            await ready.wait()
            gate.close()
            gate.cancel_inflight()
            with pytest.raises(PluginGenerationUnavailableError):
                await running
            await gate.wait_drained()
        assert len(owners) == 1 and owners[0].process.returncode is not None
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        for owner in owners:
            await owner.close()


async def test_failed_output_consumer_stops_process_without_waiting_for_deadline():
    port = PluginProcessService(GenerationGate("process-test"), frozenset({"process.spawn"}))

    async def fail(chunk):
        raise ValueError("consumer failed")

    async with asyncio.timeout(10):
        with pytest.raises(ValueError, match="consumer failed"):
            await port.run(
                ProcessRequest(
                    (sys.executable, "-c", "import time;print('ready',flush=True);time.sleep(60)")
                ),
                on_output=fail,
            )


@pytest.mark.parametrize("inherit_output", [True, False])
async def test_completed_parent_does_not_leave_an_unowned_descendant(tmp_path, inherit_output):
    port = PluginProcessService(GenerationGate("process-test"), frozenset({"process.spawn"}))
    ready, release, lock_path = (tmp_path / name for name in ("ready", "release", "owner.lock"))
    # The child holds an OS lock until it dies. A delayed marker cannot distinguish
    # a dead process from a live process that simply has not been scheduled yet.
    child = (
        "import time;from pathlib import Path;from ouro.local_locks import FileOwnerLock\n"
        f"with FileOwnerLock(Path({str(lock_path)!r}), owner={{}}):\n"
        f"    Path({str(ready)!r}).touch()\n"
        f"    while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
    )
    pipes = "" if inherit_output else ",stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL"
    parent = (
        "import subprocess,sys,time;from pathlib import Path;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}],"
        f"stdin=subprocess.DEVNULL{pipes})\n"
        f"while not Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
    )
    try:
        result = await port.run(ProcessRequest((sys.executable, "-c", parent)))
        assert result.returncode == 0 and not result.timed_out
        assert ready.exists()
        await wait_until(
            lambda: not is_lock_held(lock_path), description="descendant OS lock release"
        )
    finally:
        release.touch()
