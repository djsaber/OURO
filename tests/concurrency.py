"""Wait for observable outcomes when a tested boundary exposes no completion event."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def wait_until(
    predicate: Callable[[], bool], *, description: str, timeout: float = 30
) -> None:
    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)
    except TimeoutError:
        raise AssertionError(f"Timed out waiting for {description}") from None
