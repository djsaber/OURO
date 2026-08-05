from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from functools import partial
from typing import Any


async def invoke_callback(
    handler: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Invoke a callback without letting synchronous work block the event loop."""

    callback = partial(handler, *args, **kwargs)
    if inspect.iscoroutinefunction(handler):
        result = callback()
    else:
        result = await asyncio.to_thread(callback)
    if inspect.isawaitable(result):
        return await result
    return result


async def invoke_bounded_callback[InputT](
    handler: Callable[[InputT], Any],
    argument: InputT,
    *,
    timeout_seconds: float,
) -> Any:
    """Invoke an async callback directly or isolate a synchronous one in a thread."""

    async with asyncio.timeout(timeout_seconds):
        return await invoke_callback(handler, argument)


__all__ = ["invoke_bounded_callback", "invoke_callback"]
