from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from functools import partial
from typing import Any

from ouro.core.cancellation import settle_owned_tasks


class CallbackCancelled(RuntimeError):
    """Cancellation revoked publication; an owned callback may still have returned a value."""

    def __init__(
        self, result: object = None, *, reason: str = "callback completed after cancellation"
    ) -> None:
        super().__init__(reason)
        self.result = result
        self.reason = reason


async def invoke_callback(
    handler: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Invoke a callback without letting synchronous work block the event loop."""

    return await _invoke_callback(handler, args, kwargs, owned=False)


async def invoke_owned_callback(
    handler: Callable[..., Any],
    *args: Any,
    on_cancel: Callable[[], None] | None = None,
    **kwargs: Any,
) -> Any:
    """Own callback completion and revoke scoped access before cancellation settles."""

    return await _invoke_callback(handler, args, kwargs, owned=True, on_cancel=on_cancel)


async def run_blocking[ResultT](
    operation: Callable[..., ResultT],
    *args: Any,
    **kwargs: Any,
) -> ResultT:
    """Run one complete synchronous operation outside the event loop.

    Cancellation does not abandon the owned thread while it may still hold a
    persistence lock or mutate durable state. The caller regains cancellation
    only after the operation has settled.
    """

    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await settle_owned_tasks(task)
        if not task.cancelled():
            # Retrieve a possible exception so the task is never reported as an
            # unobserved failure. Cancellation remains the caller-visible result.
            task.exception()
        raise


async def _invoke_callback(
    handler: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    owned: bool,
    on_cancel: Callable[[], None] | None = None,
) -> Any:
    callback = partial(handler, *args, **kwargs)
    if inspect.iscoroutinefunction(handler):
        result = callback()
    elif owned:
        result = await _await_owned(
            asyncio.create_task(asyncio.to_thread(callback)),
            cancel_task=False,
            on_cancel=on_cancel,
        )
    else:
        result = await asyncio.to_thread(callback)
    if inspect.isawaitable(result):
        if not owned:
            return await result

        async def await_result() -> Any:
            return await result

        return await _await_owned(
            asyncio.create_task(await_result()), cancel_task=True, on_cancel=on_cancel
        )
    return result


async def _await_owned(
    task: asyncio.Task[Any], *, cancel_task: bool, on_cancel: Callable[[], None] | None
) -> Any:
    try:
        await asyncio.wait((task,))
        return task.result()
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        if cancel_task:
            task.cancel()
        await settle_owned_tasks(task)
        # An actual cancellation has no returned value; a callback that suppressed
        # it or finished in an owned thread retains only its observation.
        if task.cancelled():
            raise
        result = task.result()
        if inspect.iscoroutine(result):
            result.close()
            result = None
        raise CallbackCancelled(result) from None


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
    "CallbackCancelled",
    "invoke_bounded_callback",
    "invoke_callback",
    "invoke_owned_callback",
    "run_blocking",
]
