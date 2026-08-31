from __future__ import annotations

import asyncio
from threading import Event, get_ident

import pytest

from ouro.core.callbacks import run_blocking


@pytest.mark.asyncio
async def test_run_blocking_executes_outside_the_event_loop() -> None:
    loop_thread = get_ident()

    worker_thread = await run_blocking(get_ident)

    assert worker_thread != loop_thread


@pytest.mark.asyncio
async def test_run_blocking_settles_owned_work_before_propagating_cancellation() -> None:
    started = Event()
    release = Event()
    finished = Event()

    def operation() -> None:
        started.set()
        release.wait(timeout=1)
        finished.set()

    task = asyncio.create_task(run_blocking(operation))
    assert await asyncio.to_thread(started.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
