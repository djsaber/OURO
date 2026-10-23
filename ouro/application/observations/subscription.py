from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from ouro.application.contracts.base import Diagnostic
from ouro.application.contracts.execution import RuntimeActivitySnapshot, RuntimeNotification


class SessionStateSubscription(AsyncIterator[RuntimeNotification]):
    """An independent, coalesced invalidation for a Session's current state."""

    def __init__(
        self,
        session_id: str,
        *,
        event_types: frozenset[str],
        on_close: Callable[[SessionStateSubscription], None],
    ) -> None:
        self.session_id = session_id
        self.event_types = event_types
        self.initial_activity: RuntimeActivitySnapshot | None = None
        self._on_close = on_close
        self._changed = asyncio.Event()
        self._pending: RuntimeNotification | None = None
        self.closed = False

    def __aiter__(self) -> SessionStateSubscription:
        return self

    async def __anext__(self) -> RuntimeNotification:
        await self._changed.wait()
        pending = self._pending
        self._pending = None
        if not self.closed:
            self._changed.clear()
        if pending is None:
            raise StopAsyncIteration
        return pending

    async def get(self) -> RuntimeNotification:
        return await self.__anext__()

    def offer(self, notification: RuntimeNotification) -> None:
        if not self.closed:
            self._pending = notification
            self._changed.set()

    def fail(self, diagnostic: Diagnostic) -> None:
        if not self.closed:
            self.close()
            self._pending = RuntimeNotification(kind="diagnostic", diagnostic=diagnostic)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._pending = None
        self._changed.set()
        self._on_close(self)
