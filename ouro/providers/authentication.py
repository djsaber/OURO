from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

from ouro.auth.request_auth import AuthError, RequestAuthenticator, RequestAuthSnapshot
from ouro.core.errors import ConfigError
from ouro.core.provider import ProviderStreamEvent
from ouro.providers.http_transport import ProviderTransportError


@dataclass(frozen=True)
class ProviderRequestPolicy:
    """Service-specific error facts, supplied by composition rather than wire codecs."""

    quota_error_codes: frozenset[str] = frozenset()


class ProviderRequestError(ProviderTransportError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str | None,
        retryable: bool = False,
        retry_after: float | None = None,
        recovery: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        super().__init__(message, retryable=retryable, retry_after=retry_after)
        self.error_code = error_code
        self.recovery = recovery


def request_failure(
    error: ProviderTransportError,
    *,
    policy: ProviderRequestPolicy,
    managed: bool,
    recovery: Callable[[], Awaitable[bool]] | None = None,
) -> ProviderRequestError:
    code = None
    message = (
        f"Model service returned HTTP {error.status_code}."
        if managed and error.status_code is not None
        else "Model request failed."
        if managed
        else str(error)
    )
    retryable = error.retryable
    if error.status_code == 401:
        code, message, retryable = (
            "authentication_required",
            "Model service rejected authentication.",
            False,
        )
    elif error.status_code == 403:
        code, message, retryable = "access_denied", "Model service denied access.", False
    elif error.status_code == 429:
        if error.service_error_code in policy.quota_error_codes:
            code, message, retryable = "quota_exhausted", "Model service quota is exhausted.", False
        else:
            code, message = "rate_limited", "Model service rate limit reached."
    return ProviderRequestError(
        message,
        error_code=code,
        retryable=retryable,
        retry_after=error.retry_after,
        recovery=recovery if error.status_code == 401 else None,
    )


@asynccontextmanager
async def request_authentication(
    authenticator: RequestAuthenticator,
    *,
    url: str,
    headers: Mapping[str, str],
    policy: ProviderRequestPolicy,
) -> AsyncIterator[RequestAuthSnapshot]:
    snapshot = await authenticator.acquire(url)
    normalized = {name.lower(): value for name, value in headers.items()}
    auth_headers = {name.lower(): value for name, value in snapshot.headers.items()}
    if normalized.keys() & auth_headers.keys():
        raise ConfigError("default headers must not override request authentication")
    # Host routing and common alternative credentials must not bypass the managed
    # target or introduce a second identity alongside the driver's own headers.
    if snapshot.revision is not None and normalized.keys() & {
        "host",
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "cookie",
    }:
        raise ConfigError("managed requests do not accept routing or credential header overrides")
    prepared = RequestAuthSnapshot(
        {**normalized, **auth_headers}, revision=snapshot.revision, scope=snapshot.scope
    )

    async def recover() -> bool:
        return await authenticator.recover(snapshot)

    try:
        yield prepared
    except ProviderTransportError as error:
        raise request_failure(
            error, policy=policy, managed=snapshot.revision is not None, recovery=recover
        ) from None
    except AuthError, ConfigError:
        raise
    except Exception:
        if snapshot.revision is not None:
            raise ProviderRequestError(
                "Model response could not be processed.", error_code=None
            ) from None
        raise


def safe_response_event(
    event: ProviderStreamEvent, snapshot: RequestAuthSnapshot
) -> ProviderStreamEvent:
    if snapshot.revision is not None and event.type == "error":
        # Wire error strings may echo credentials. Only classified safe facts may
        # leave a managed request; successful content remains the codec's output.
        return event.model_copy(
            update={"error": "Model service returned an invalid or failed response."}
        )
    return event
