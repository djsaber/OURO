from __future__ import annotations

import asyncio
from collections.abc import Callable


class AbortSignal:
    def __init__(self, checker: Callable[[], bool] | None = None) -> None:
        self._checker = checker
        self._aborted = False
        self._reason = "User aborted"

    @property
    def reason(self) -> str:
        return self._reason

    def abort(self, reason: str = "User aborted") -> None:
        self._aborted = True
        self._reason = reason

    def is_aborted(self) -> bool:
        if self._aborted:
            return True
        return bool(self._checker and self._checker())

    async def wait(self, *, poll_interval: float = 0.05) -> None:
        """Wait until local or durable cancellation has been requested."""

        while not self.is_aborted():
            await asyncio.sleep(poll_interval)


def is_aborted(signal: object | None) -> bool:
    return isinstance(signal, AbortSignal) and signal.is_aborted()
