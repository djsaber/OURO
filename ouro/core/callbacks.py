from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any


async def invoke_bounded_callback[InputT](
    handler: Callable[[InputT], Any],
    argument: InputT,
    *,
    timeout_seconds: float,
) -> Any:
    """Invoke an async callback directly or isolate a synchronous one in a thread."""

    async with asyncio.timeout(timeout_seconds):
        if inspect.iscoroutinefunction(handler):
            return await handler(argument)
        result = await asyncio.to_thread(handler, argument)
        if inspect.isawaitable(result):
            return await result
        return result


__all__ = ["invoke_bounded_callback"]
