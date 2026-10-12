from __future__ import annotations

import asyncio
import sys

import pytest

import ouro.local_processes as processes


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
