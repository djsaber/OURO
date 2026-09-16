from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from datetime import datetime

from ouro.auth.contracts import AuthError, CredentialGrant
from ouro.auth.driver_contracts import AuthorizationChallenge
from ouro.core.base import utc_now


class TaskAuthorizationAttempt:
    """Own a bounded authorization exchange and exactly one resource cleanup."""

    def __init__(
        self,
        challenge: AuthorizationChallenge,
        *,
        authorize: Callable[[], Awaitable[CredentialGrant]],
        cleanup: Callable[[], Awaitable[None]],
        clock: Callable[[], datetime] = utc_now,
        cleanup_timeout: float = 5.0,
    ) -> None:
        if not math.isfinite(cleanup_timeout) or cleanup_timeout <= 0:
            raise ValueError("authorization cleanup timeout must be positive")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (challenge.expires_at - clock()).total_seconds()
        self._challenge = challenge
        self._exchange = asyncio.create_task(self._authorize(authorize, deadline))
        # Keep cleanup outside the cancelled exchange. Even cancellation before
        # its first instruction must release resources already opened by begin.
        self._completion = asyncio.create_task(self._finish(cleanup, cleanup_timeout))
        self._completion.add_done_callback(self._completed)

    @property
    def challenge(self) -> AuthorizationChallenge:
        return self._challenge

    async def wait(self) -> CredentialGrant:
        await asyncio.shield(self._completion)
        if self._exchange.cancelled():
            raise AuthError("authorization_cancelled")
        return self._exchange.result()

    async def cancel(self) -> None:
        await self.close()

    async def close(self) -> None:
        # Cancel once: repeated close must not interrupt a driver's finally block.
        if not self._exchange.done() and not self._exchange.cancelling():
            self._exchange.cancel()
        while True:
            try:
                await asyncio.shield(self._completion)
                return
            except asyncio.CancelledError:
                if self._completion.done():
                    self._completion.result()
                    return

    async def _authorize(
        self, authorize: Callable[[], Awaitable[CredentialGrant]], deadline: float
    ) -> CredentialGrant:
        timer = asyncio.timeout_at(deadline)
        try:
            if asyncio.get_running_loop().time() >= deadline:
                raise AuthError("authorization_expired")
            async with timer:
                grant = await authorize()
            # A driver's cancellation handler must not turn an expired or
            # cancelled exchange into a grant that Application could commit.
            if timer.expired() or asyncio.get_running_loop().time() >= deadline:
                raise AuthError("authorization_expired")
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise AuthError("authorization_cancelled")
            return grant
        except TimeoutError:
            raise AuthError(
                "authorization_expired" if timer.expired() else "authorization_failed"
            ) from None
        except AuthError:
            raise
        except Exception:
            # Driver failures can contain callback URLs or raw token responses.
            raise AuthError("authorization_failed") from None

    async def _finish(self, cleanup: Callable[[], Awaitable[None]], cleanup_timeout: float) -> None:
        try:
            await asyncio.gather(self._exchange, return_exceptions=True)
        finally:
            timer = asyncio.timeout(cleanup_timeout)
            try:
                async with timer:
                    await cleanup()
                if timer.expired():
                    raise AuthError("authorization_cleanup_failed")
            except Exception, asyncio.CancelledError:
                raise AuthError("authorization_cleanup_failed") from None

    @staticmethod
    def _completed(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            # Retrieve unattended failures; wait/close can still observe them.
            task.exception()
