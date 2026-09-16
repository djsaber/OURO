from __future__ import annotations

import asyncio
from threading import Event, Lock
from time import monotonic

from ouro.auth.contracts import AuthError, AuthErrorCode


class CommitAdmission:
    """Arbitrate cancellation/closure against the start of one synchronous save."""

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.cancel_event = Event()
        self._lock = Lock()
        self._started = False
        self._reason: AuthErrorCode | None = None

    def cancel(self, reason: AuthErrorCode | None = None) -> None:
        with self._lock:
            if not self._started and not self.cancel_event.is_set():
                self._reason = reason
                self.cancel_event.set()

    def check(self) -> float:
        with self._lock:
            return self._remaining()

    def start(self) -> None:
        with self._lock:
            self._remaining()
            self._started = True

    def _remaining(self) -> float:
        if self.cancel_event.is_set():
            if self._reason is not None:
                raise AuthError(self._reason)
            raise asyncio.CancelledError
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise AuthError("credential_lock_timeout")
        return remaining
