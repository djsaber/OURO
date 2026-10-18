from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ouro.local_processes as processes


@pytest.mark.parametrize("remaining_group", ["gone", "descendants", "denied"])
async def test_group_permission_race_reaps_leader_and_preserves_descendant_cleanup(
    monkeypatch, remaining_group
):
    process = SimpleNamespace(pid=123456, returncode=None)

    async def reap():
        process.returncode = 0
        return 0

    process.wait = AsyncMock(side_effect=reap)
    signals = []

    def signal_group(pid, signal):
        assert (pid, signal) == (-process.pid, 9)
        signals.append(pid)
        if len(signals) == 1:
            raise PermissionError("group contains only an unreaped leader")
        assert process.returncode == 0
        if remaining_group == "gone":
            raise ProcessLookupError
        if remaining_group == "denied":
            raise PermissionError("live descendants still deny termination")

    monkeypatch.setattr(processes.os, "kill", signal_group)
    owner = processes.LocalProcess(process, None)
    if remaining_group == "denied":
        with pytest.raises(PermissionError, match="live descendants"):
            await owner.terminate()
    else:
        await owner.terminate()
    assert len(signals) == 2
    process.wait.assert_awaited_once()


async def test_group_permission_failure_still_reports_a_live_leader(monkeypatch):
    process = SimpleNamespace(pid=123456, returncode=None)
    process.wait = AsyncMock(side_effect=TimeoutError)

    def denied(_pid, _signal):
        raise PermissionError("live group denies termination")

    monkeypatch.setattr(processes.os, "kill", denied)
    with pytest.raises(PermissionError, match="live group"):
        await processes.LocalProcess(process, None).terminate()


async def test_cancellation_during_spawn_retains_and_closes_the_created_process(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    owners = []
    start = processes._start_process

    async def delayed_start(arguments, *, shell, options):
        owner = await start(arguments, shell=shell, options=options)
        owners.append(owner)
        started.set()
        await release.wait()
        return owner

    monkeypatch.setattr(processes, "_start_process", delayed_start)
    spawning = asyncio.create_task(
        processes.start_process(sys.executable, "-c", "import time;time.sleep(60)")
    )
    try:
        async with asyncio.timeout(10):
            await started.wait()
            spawning.cancel()
            await asyncio.sleep(0)
            assert not spawning.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await spawning
        assert owners[0].process.returncode is not None
        if owners[0].job is not None:
            assert owners[0].job.handle is None
    finally:
        release.set()
        await asyncio.gather(spawning, return_exceptions=True)
        for owner in owners:
            await owner.close()


async def test_repeated_cancellation_does_not_abandon_process_close(monkeypatch):
    owner = await processes.start_process(sys.executable, "-c", "import time;time.sleep(60)")
    entered, release = asyncio.Event(), asyncio.Event()
    terminate = owner.terminate

    async def delayed_terminate():
        entered.set()
        await release.wait()
        await terminate()

    monkeypatch.setattr(owner, "terminate", delayed_terminate)
    closing = asyncio.create_task(owner.close())
    try:
        await entered.wait()
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert owner.process.returncode is not None
    finally:
        release.set()
        await owner.close()
