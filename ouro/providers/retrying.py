from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable

from ouro.auth.request_auth import AuthError
from ouro.core.provider import ProviderRetryNotice, ProviderRetryPolicy, ProviderStreamEvent
from ouro.providers.authentication import ProviderRequestError
from ouro.providers.http_transport import ProviderTransportError

ProviderStreamFactory = Callable[[], AsyncIterator[ProviderStreamEvent]]


async def stream_with_retries(
    stream_once: ProviderStreamFactory,
    retry_policy: ProviderRetryPolicy,
    *,
    include_reasoning: bool = True,
) -> AsyncGenerator[ProviderStreamEvent]:
    """One model-attempt budget, with at most one pre-output authentication recovery."""
    attempt = 1
    recovered = False
    while True:
        emitted_response_event = False
        try:
            events = stream_once()
            try:
                async for event in events:
                    emitted_response_event = True
                    # Hidden model output still commits this attempt: visibility
                    # must never make a partially generated response replayable.
                    if event.type == "reasoning_delta" and not include_reasoning:
                        continue
                    yield event
            finally:
                close = getattr(events, "aclose", None)
                if close is not None:
                    await close()
            return
        except Exception as exc:  # noqa: BLE001 - provider failures are normalized events.
            next_attempt = attempt + 1
            retry = False
            delay = 0.0
            if not emitted_response_event and next_attempt <= retry_policy.max_attempts:
                if isinstance(exc, ProviderRequestError) and exc.recovery is not None:
                    if not recovered:
                        recovered = True
                        try:
                            retry = await exc.recovery()
                        except AuthError as recovery_error:
                            exc = recovery_error
                        except Exception:
                            exc = AuthError("credential_refresh_failed")
                elif isinstance(exc, ProviderTransportError):
                    retry = exc.retryable
                    delay = retry_policy.delay_before_attempt(next_attempt)
                    if exc.retry_after is not None:
                        # Do not retry earlier than requested or wait without bound.
                        retry = retry and exc.retry_after <= retry_policy.max_backoff_seconds
                        delay = max(delay, exc.retry_after)
            if not retry:
                code = (
                    exc.code
                    if isinstance(exc, AuthError)
                    else exc.error_code
                    if isinstance(exc, ProviderRequestError)
                    else None
                )
                yield ProviderStreamEvent(
                    type="error", error=str(exc) or type(exc).__name__, error_code=code
                )
                return
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
