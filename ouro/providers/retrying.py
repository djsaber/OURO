from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from ouro.core.provider import (
    ProviderRetryNotice,
    ProviderRetryPolicy,
    ProviderStreamEvent,
)
from ouro.providers.http_transport import ProviderTransportError

ProviderStreamFactory = Callable[[], AsyncIterator[ProviderStreamEvent]]


async def stream_with_retries(
    stream_once: ProviderStreamFactory,
    retry_policy: ProviderRetryPolicy,
) -> AsyncIterator[ProviderStreamEvent]:
    """Apply the shared pre-output retry contract to one provider request."""

    attempt = 1
    while True:
        emitted_response_event = False
        try:
            async for event in stream_once():
                emitted_response_event = True
                yield event
            return
        except Exception as exc:  # noqa: BLE001 - provider failures are normalized events.
            next_attempt = attempt + 1
            if not _can_retry(
                exc,
                next_attempt=next_attempt,
                emitted_response_event=emitted_response_event,
                retry_policy=retry_policy,
            ):
                yield ProviderStreamEvent(type="error", error=str(exc) or type(exc).__name__)
                return
            delay = retry_policy.delay_before_attempt(next_attempt)
            yield ProviderStreamEvent(
                type="retry_scheduled",
                retry=ProviderRetryNotice(
                    attempt=next_attempt,
                    max_attempts=retry_policy.max_attempts,
                    delay_seconds=delay,
                    reason=str(exc),
                ),
            )
            if delay:
                await asyncio.sleep(delay)
            attempt = next_attempt


def _can_retry(
    exc: Exception,
    *,
    next_attempt: int,
    emitted_response_event: bool,
    retry_policy: ProviderRetryPolicy,
) -> bool:
    return (
        isinstance(exc, ProviderTransportError)
        and exc.retryable
        and not emitted_response_event
        and next_attempt <= retry_policy.max_attempts
    )
