from __future__ import annotations

import asyncio
import sys

import pytest

from ouro.plugins.catalog import GenerationGate, PluginGenerationUnavailableError
from ouro.plugins.invocation import plugin_invocation
from ouro.plugins.processes import PluginProcessService
from ouro.sdk.plugins.processes import ProcessRequest


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


async def test_generation_revocation_stops_streaming_process_before_returning(tmp_path):
    gate = GenerationGate("process-test")
    port = PluginProcessService(gate, frozenset({"process.spawn"}))
    ready = asyncio.Event()
    marker = tmp_path / "late.txt"

    async def output(chunk):
        ready.set()

    code = (
        "import time,pathlib;print('ready',flush=True);time.sleep(1);"
        f"pathlib.Path({str(marker)!r}).write_text('late')"
    )
    running = asyncio.create_task(
        port.run(ProcessRequest((sys.executable, "-c", code)), on_output=output)
    )
    async with asyncio.timeout(10):
        await ready.wait()
        gate.close()
        gate.cancel_inflight()
        with pytest.raises(PluginGenerationUnavailableError):
            await running
        await gate.wait_drained()
    await asyncio.sleep(1.1)
    assert not marker.exists()


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
    late = tmp_path / "orphan"
    child = f"import time,pathlib;time.sleep(1);pathlib.Path({str(late)!r}).touch()"
    pipes = "" if inherit_output else ",stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL"
    parent = (
        "import subprocess,sys;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}],"
        f"stdin=subprocess.DEVNULL{pipes})"
    )
    result = await port.run(ProcessRequest((sys.executable, "-c", parent)))
    assert result.returncode == 0
    await asyncio.sleep(1.1)
    assert not late.exists()
