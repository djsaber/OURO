from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncGenerator, Iterator, Mapping
from contextlib import aclosing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.request import getproxies, proxy_bypass

import httpx

_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_ROUTE_FAILURES = (
    TimeoutError,
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.ProxyError,
    httpx.RemoteProtocolError,
)


class ProviderTransportError(RuntimeError):
    """HTTP provider failure with an explicit retry classification."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        retry_after: float | None = None,
        service_error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after
        self.service_error_code = service_error_code


class ProviderResponseTimeout(ProviderTransportError):
    """No response event arrived within the transport's wait budget."""

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Model response timed out after {timeout_seconds:g} seconds of inactivity.",
            retryable=True,
        )


class ProviderHTTPTransport(Protocol):
    """Bound response waits; streaming transports renew the budget for each SSE item."""

    async def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> dict[str, Any]: ...


@dataclass
class _ProxyPreference:
    proxy: str
    prefer_direct: bool = False


class HttpxProviderHTTPTransport:
    """Shared JSON and SSE transport with request-time network route selection."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._provided_client = client
        self._clients: dict[str | None, httpx.AsyncClient] = {}
        self._preferences: dict[tuple[str, bytes], _ProxyPreference] = {}

    def _preference_for(self, url: str) -> _ProxyPreference | None:
        target = httpx.URL(url)
        origin = (target.scheme, target.netloc)
        proxies = getproxies()
        proxy = proxies.get(target.scheme) or proxies.get("all")
        if not proxy or proxy_bypass(target.netloc.decode("ascii")):
            self._preferences.pop(origin, None)
            return None
        if "://" not in proxy:
            proxy = f"http://{proxy}"

        preference = self._preferences.get(origin)
        if preference is None or preference.proxy != proxy:
            preference = self._preferences[origin] = _ProxyPreference(proxy)
        return preference

    @contextmanager
    def _request_client(self, url: str, timeout: float) -> Iterator[httpx.AsyncClient]:
        _validate_timeout(timeout)
        client = self._provided_client
        preference = None
        proxy = None
        if client is None:
            preference = self._preference_for(url)
            if preference is not None and not preference.prefer_direct:
                proxy = preference.proxy
            client = self._clients.get(proxy)
            if client is None:
                client = httpx.AsyncClient(
                    transport=httpx.AsyncHTTPTransport(proxy=proxy), trust_env=False
                )
                self._clients[proxy] = client
        try:
            yield client
        except (httpx.RequestError, TimeoutError) as exc:
            if preference is not None and isinstance(exc, _ROUTE_FAILURES):
                preference.prefer_direct = proxy is not None
            if isinstance(exc, TimeoutError):
                raise ProviderResponseTimeout(timeout) from None
            raise _provider_network_error(exc) from exc

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
        self._preferences.clear()

    async def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        request_headers = {
            "content-type": "application/json",
            "accept": "application/json",
            **headers,
        }
        with self._request_client(url, timeout) as client:
            async with asyncio.timeout(timeout):
                response = await client.post(
                    url,
                    headers=request_headers,
                    json=dict(payload),
                    timeout=timeout,
                    follow_redirects=False,
                )
        if not response.is_success:
            raise _http_status_error(response)

        if not response.content:
            return {}
        try:
            parsed = response.json()
        except ValueError as exc:
            raise ProviderTransportError(
                "provider response was not valid JSON",
                retryable=False,
            ) from exc
        if not isinstance(parsed, dict):
            raise ProviderTransportError(
                "provider response JSON must be an object",
                retryable=False,
            )
        return parsed

    async def stream_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> AsyncGenerator[Mapping[str, Any]]:
        request_headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
            **headers,
        }
        with self._request_client(url, timeout) as client:
            items = _stream_request(client, url, request_headers, payload, timeout)
            async with aclosing(items):
                while True:
                    try:
                        async with asyncio.timeout(timeout):
                            item = await anext(items)
                    except StopAsyncIteration:
                        return
                    yield item


def _validate_timeout(timeout: float) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")


async def _stream_request(
    client: httpx.AsyncClient,
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: float,
) -> AsyncGenerator[Mapping[str, Any]]:
    # Complete SSE events, not socket bytes or comment heartbeats, establish
    # response progress. The caller owns that wait timer; httpx bounds setup I/O.
    async with client.stream(
        "POST",
        url,
        headers=dict(headers),
        json=dict(payload),
        timeout=httpx.Timeout(connect=timeout, read=None, write=timeout, pool=timeout),
        follow_redirects=False,
    ) as response:
        async with aclosing(_stream_sse_json(response)) as items:
            async for item in items:
                yield item


def _http_status_error(response: httpx.Response) -> ProviderTransportError:
    body = " ".join(response.text.split())[:500]
    return ProviderTransportError(
        f"provider HTTP {response.status_code}: {body}",
        retryable=response.status_code in _RETRYABLE_STATUS_CODES,
        status_code=response.status_code,
        retry_after=_retry_after(response),
        service_error_code=_service_error_code(response),
    )


def _provider_network_error(exc: httpx.RequestError) -> ProviderTransportError:
    if isinstance(exc, httpx.TimeoutException):
        return ProviderTransportError("provider request timed out", retryable=True)
    detail = str(exc).strip()
    reason = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return ProviderTransportError(f"provider connection failed: {reason}", retryable=True)


async def _stream_sse_json(response: httpx.Response) -> AsyncGenerator[Mapping[str, Any]]:
    if not response.is_success:
        await response.aread()
        raise _http_status_error(response)

    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data_lines:
                parsed = _parse_sse_data("\n".join(data_lines))
                data_lines = []
                if parsed is None:
                    return
                yield parsed
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    if data_lines:
        parsed = _parse_sse_data("\n".join(data_lines))
        if parsed is not None:
            yield parsed


def _parse_sse_data(data: str) -> Mapping[str, Any] | None:
    if data.strip() == "[DONE]":
        return None
    try:
        parsed = json.loads(data)
    except ValueError as exc:
        raise ProviderTransportError(
            "provider stream chunk was not valid JSON",
            retryable=False,
        ) from exc
    if not isinstance(parsed, Mapping):
        raise ProviderTransportError(
            "provider stream chunk JSON must be an object",
            retryable=False,
        )
    return parsed


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except ValueError, TypeError, OverflowError:
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _service_error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None
    code = error.get("code") or error.get("type")
    return code if isinstance(code, str) and len(code) <= 128 else None
