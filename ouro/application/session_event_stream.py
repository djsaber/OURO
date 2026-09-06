from __future__ import annotations

import asyncio
from collections.abc import Callable

from ouro.application.dto import Diagnostic
from ouro.core.callbacks import run_blocking
from ouro.core.events import EventRecord
from ouro.sessions.base import SessionStore


class SessionEventStream:
    """Merge local pushes and durable arrivals into one ordered Session stream.

    One instance is shared by a supervisor's subscribers to a Session. Local
    contiguous events are delivered immediately; gaps and other processes'
    events are read in bounded pages, independently of the execution owner.
    """

    def __init__(
        self,
        store: SessionStore,
        session_id: str,
        *,
        after_sequence: int,
        publish: Callable[[EventRecord], None],
        report: Callable[[Diagnostic], None],
        is_running: Callable[[], bool],
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._publish = publish
        self._report = report
        self._is_running = is_running
        self._sequence = after_sequence
        self._wake = asyncio.Event()
        self._closed = False

    def close(self) -> None:
        self._closed = True
        self._wake.set()

    def offer(self, event: EventRecord) -> None:
        if self._closed or event.sequence <= self._sequence:
            return
        if event.sequence != self._sequence + 1:
            # Never advance past an unseen external event. The durable page
            # includes this local event too, so no second pending buffer is needed.
            self._wake.set()
            return
        self._sequence = event.sequence
        self._publish(event)

    async def follow(self) -> None:
        try:
            while not self._closed:
                self._wake.clear()
                events, truncated = await run_blocking(
                    self._store.load_events_after,
                    self._session_id,
                    after_sequence=self._sequence,
                    limit=256,
                )
                if self._closed:
                    return
                for event in events:
                    # The store establishes order for the whole page. Unlike
                    # a local push, it also proves there is no unseen event in
                    # a sequence gap (imported histories may be non-contiguous).
                    if event.sequence > self._sequence:
                        self._sequence = event.sequence
                        self._publish(event)
                if truncated:
                    # The next page yields to the loop before delivering more.
                    continue
                try:
                    async with asyncio.timeout(0.1 if self._is_running() else 0.5):
                        await self._wake.wait()
                except TimeoutError:
                    pass
        except Exception as exc:  # noqa: BLE001 - report persistence failures to the host.
            if not self._closed:
                self._report(
                    Diagnostic(
                        code="runtime_subscription_read_failed",
                        severity="error",
                        component="runtime.supervisor",
                        message=f"Cannot follow Session events ({type(exc).__name__}).",
                        session_id=self._session_id,
                        recoverable=True,
                        suggested_action="Reload the Session to restore observation.",
                    )
                )
        finally:
            self.close()


__all__ = ["SessionEventStream"]
