from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from ouro.auth.request_auth import AuthError, RequestAuthenticator, RequestAuthSnapshot
from ouro.core.billing import BillingMode
from ouro.core.errors import ConfigError
from ouro.core.provider import ProviderStreamEvent
from ouro.providers.http_transport import ProviderResponseTimeout, ProviderTransportError

_SAFE_NETWORK_ERRORS = (
    (httpx.ConnectTimeout, "connect_timeout", "Model connection timed out (ConnectTimeout)."),
    (httpx.ReadTimeout, "read_timeout", "Model response read timed out (ReadTimeout)."),
    (httpx.WriteTimeout, "write_timeout", "Model request write timed out (WriteTimeout)."),
    (httpx.PoolTimeout, "pool_timeout", "Model connection pool wait timed out (PoolTimeout)."),
    (httpx.TimeoutException, "timeout", "Model network request timed out."),
    (
        httpx.ConnectError,
        "connection_failed",
        "Model connection could not be established (ConnectError).",
    ),
    (httpx.ReadError, "read_error", "Model response could not be read (ReadError)."),
    (httpx.WriteError, "write_error", "Model request could not be sent (WriteError)."),
    (httpx.ProxyError, "proxy_error", "Model proxy connection failed (ProxyError)."),
    (httpx.ProtocolError, "protocol_error", "Model HTTP protocol error (ProtocolError)."),
    (httpx.RequestError, "network_error", "Model network request failed."),
)


@dataclass(frozen=True)
class ProviderRequestPolicy:
    """Service facts supplied by trusted composition rather than wire codecs or login type."""

    quota_error_codes: frozenset[str] = frozenset()
    billing_mode: BillingMode = "unknown"
    # False means max_output_tokens remains a local planning budget only.
    supports_output_limit: bool = True


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
    redact_errors: bool,
    recovery: Callable[[], Awaitable[bool]] | None = None,
) -> ProviderRequestError:
    code = "provider_request_failed" if redact_errors else None
    message = (
        f"Model service returned HTTP {error.status_code}."
        if redact_errors and error.status_code is not None
        else "Model request failed."
        if redact_errors
        else str(error)
    )
    safe_failure = _safe_transport_failure(error)
    if safe_failure is not None:
        code, safe_message = safe_failure
        if redact_errors:
            message = safe_message
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


def _safe_transport_failure(error: ProviderTransportError) -> tuple[str, str] | None:
    if isinstance(error, ProviderResponseTimeout):
        return "provider_response_timeout", (
            f"Model response timed out after {error.timeout_seconds:g} seconds "
            "(total request timeout)."
        )
    # The transport chains the actual httpx failure, including the final route
    # after fallback. Expose only known class names, never messages or URLs.
    if error.status_code is None:
        for exception_type, code, message in _SAFE_NETWORK_ERRORS:
            if isinstance(error.__cause__, exception_type):
                return "provider_" + code, message
    return None


@asynccontextmanager
async def request_authentication(
    authenticator: RequestAuthenticator,
    *,
    url: str,
    headers: Mapping[str, str],
    policy: ProviderRequestPolicy,
    redact_errors: bool = False,
) -> AsyncGenerator[RequestAuthSnapshot]:
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
            error,
            policy=policy,
            redact_errors=snapshot.revision is not None or redact_errors,
            recovery=recover,
        ) from None
    except AuthError, ConfigError:
        raise
    except Exception:
        if snapshot.revision is not None or redact_errors:
            raise ProviderRequestError(
                "Model response could not be processed.", error_code="provider_response_failed"
            ) from None
        raise


def safe_response_event(
    event: ProviderStreamEvent, snapshot: RequestAuthSnapshot, *, redact_errors: bool = False
) -> ProviderStreamEvent:
    if (snapshot.revision is not None or redact_errors) and event.type == "error":
        # Wire error strings may echo credentials. Only classified safe facts may
        # leave a request carrying private headers; successful content remains
        # the codec's output.
        return event.model_copy(
            update={
                "error": "Model service returned an invalid or failed response.",
                "error_code": event.error_code or "provider_response_failed",
            }
        )
    return event
