from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


@asynccontextmanager
async def running_runtime_task(runtime, operation):
    """Own a gated run through assertions, including failure-path cleanup.

    Events and persisted state establish ordering. The overall deadline only
    detects a deadlock; individual disk operations have no latency contract.
    """
    task = asyncio.create_task(operation)
    try:
        async with asyncio.timeout(30):
            yield task
    except TimeoutError:
        raise AssertionError(f"Runtime scenario did not settle; task={task!r}") from None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()
