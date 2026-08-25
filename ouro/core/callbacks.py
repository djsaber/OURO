from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from functools import partial
from typing import Any

from ouro.core.cancellation import settle_owned_tasks


class CallbackOutcomeUnknown(RuntimeError):
    """A synchronous callback finished only after its caller was cancelled."""


async def invoke_callback(
    handler: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Invoke a callback without letting synchronous work block the event loop."""

    return await _invoke_callback(handler, args, kwargs, own_sync_cancellation=False)


async def invoke_owned_callback(
    handler: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Invoke a callback while retaining ownership of cancelled synchronous work."""

    return await _invoke_callback(handler, args, kwargs, own_sync_cancellation=True)


async def _invoke_callback(
    handler: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    own_sync_cancellation: bool,
) -> Any:
    callback = partial(handler, *args, **kwargs)
    if inspect.iscoroutinefunction(handler):
        result = callback()
    else:
        thread = asyncio.create_task(asyncio.to_thread(callback))
        if not own_sync_cancellation:
            result = await thread
        else:
            try:
                result = await asyncio.shield(thread)
            except asyncio.CancelledError:
                await settle_owned_tasks(thread)
                result = thread.result()
                if inspect.iscoroutine(result):
                    result.close()
                raise CallbackOutcomeUnknown from None
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


__all__ = [
    "CallbackOutcomeUnknown",
    "invoke_bounded_callback",
    "invoke_callback",
    "invoke_owned_callback",
]
