from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.request import getproxies

import httpx

_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_HTTP_PROXY_KEYS = frozenset({"all", "http", "https"})
_PROXY_PROFILE_KEYS = _HTTP_PROXY_KEYS | {"no"}

type _ProxyProfile = tuple[tuple[str, str], ...]
type _NetworkRoute = tuple[str, httpx.AsyncClient]


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


class ProviderHTTPTransport(Protocol):
    async def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> dict[str, Any]: ...


class HttpxProviderHTTPTransport:
    """Shared JSON and SSE transport with request-time network route selection."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client
        self._owns_client = client is None
        self._system_clients: dict[_ProxyProfile, httpx.AsyncClient] = {}
        self._direct_client: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if not self._owns_client:
            assert self.client is not None
            return self.client
        return self._system_client(_proxy_profile())

    def _system_client(self, profile: _ProxyProfile) -> httpx.AsyncClient:
        client = self._system_clients.get(profile)
        if client is None:
            # httpx resolves environment and OS proxy settings when the client is
            # constructed. Keep one connection pool per observed profile so a
            # long-running Host follows proxy changes without sacrificing reuse.
            client = httpx.AsyncClient()
            self._system_clients[profile] = client
        self.client = client
        return client

    def _clients_for_request(self) -> tuple[_NetworkRoute, ...]:
        if not self._owns_client:
            return (("configured client", self._client()),)

        profile = _proxy_profile()
        system_client = self._system_client(profile)
        if not _profile_has_http_proxy(profile):
            return (("system network", system_client),)

        if self._direct_client is None:
            # Disabling trust_env removes proxy discovery. Build the SSL context
            # separately so enterprise SSL_CERT_FILE/SSL_CERT_DIR settings remain
            # authoritative on the direct fallback route.
            ssl_context = httpx.create_ssl_context(trust_env=True)
            self._direct_client = httpx.AsyncClient(verify=ssl_context, trust_env=False)
        return (
            ("system proxy", system_client),
            ("direct network", self._direct_client),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            clients = {id(client): client for client in self._system_clients.values()}
            if self._direct_client is not None:
                clients[id(self._direct_client)] = self._direct_client
            for client in clients.values():
                await client.aclose()
            self._system_clients.clear()
            self._direct_client = None
            self.client = None

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
        routes = self._clients_for_request()
        failures: list[tuple[str, httpx.RequestError]] = []
        for index, (route, client) in enumerate(routes):
            try:
                response = await client.post(
                    url,
                    headers=dict(request_headers),
                    json=dict(payload),
                    timeout=timeout,
                    follow_redirects=False,
                )
                response.raise_for_status()
                break
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                raise ProviderTransportError(
                    f"provider HTTP {status_code}: {_compact_body(exc.response.text)}",
                    retryable=status_code in _RETRYABLE_STATUS_CODES,
                    status_code=status_code,
                    retry_after=_retry_after(exc.response),
                    service_error_code=_service_error_code(exc.response),
                ) from exc
            except httpx.RequestError as exc:
                failures.append((route, exc))
                if index + 1 < len(routes):
                    continue
                raise _provider_network_error(failures) from exc
        else:  # pragma: no cover - every failed route raises above.
            raise AssertionError("provider request exhausted network routes")

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
    ) -> AsyncIterator[Mapping[str, Any]]:
        request_headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
            **headers,
        }
        stream_timeout = httpx.Timeout(
            connect=timeout,
            read=None,
            write=timeout,
            pool=timeout,
        )
        routes = self._clients_for_request()
        failures: list[tuple[str, httpx.RequestError]] = []
        for index, (route, client) in enumerate(routes):
            emitted_item = False
            try:
                async with client.stream(
                    "POST",
                    url,
                    headers=dict(request_headers),
                    json=dict(payload),
                    timeout=stream_timeout,
                    follow_redirects=False,
                ) as response:
                    async for item in _stream_sse_json(response):
                        emitted_item = True
                        yield item
                return
            except httpx.RequestError as exc:
                failures.append((route, exc))
                if not emitted_item and index + 1 < len(routes):
                    continue
                raise _provider_network_error(failures) from exc


def _compact_body(body: str) -> str:
    return " ".join(body.split())[:500]


def _describe_request_error(exc: httpx.RequestError) -> str:
    """Render an httpx transport failure whose meaning lives in its type.

    Connection-drop errors (``ReadError``, ``ConnectError``, ``RemoteProtocolError``,
    ...) frequently stringify to an empty message, which would otherwise erase the
    failure detail. Fall back to the exception type so the boundary always yields a
    diagnosable reason.
    """

    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _provider_network_error(
    failures: list[tuple[str, httpx.RequestError]],
) -> ProviderTransportError:
    if len(failures) == 1:
        exc = failures[0][1]
        if isinstance(exc, httpx.TimeoutException):
            return ProviderTransportError("provider request timed out", retryable=True)
        return ProviderTransportError(
            f"provider connection failed: {_describe_request_error(exc)}",
            retryable=True,
        )

    details = "; ".join(f"{route}: {_describe_request_error(exc)}" for route, exc in failures)
    return ProviderTransportError(
        f"provider connection failed after route fallback: {details}",
        retryable=True,
    )


def _proxy_profile() -> _ProxyProfile:
    """Snapshot the proxy inputs httpx consumes from the environment or OS."""

    return tuple(
        sorted(
            (str(key).lower(), str(value).strip())
            for key, value in getproxies().items()
            if str(key).lower() in _PROXY_PROFILE_KEYS and value
        )
    )


def _profile_has_http_proxy(profile: _ProxyProfile) -> bool:
    return any(key in _HTTP_PROXY_KEYS for key, _ in profile)


async def _stream_sse_json(response: httpx.Response) -> AsyncIterator[Mapping[str, Any]]:
    if response.status_code >= 300:
        body = (await response.aread()).decode(errors="replace")
        raise ProviderTransportError(
            f"provider HTTP {response.status_code}: {_compact_body(body)}",
            retryable=response.status_code in _RETRYABLE_STATUS_CODES,
            status_code=response.status_code,
            retry_after=_retry_after(response),
            service_error_code=_service_error_code(response),
        )

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
    # Internal comparison only: never copy a supplier's arbitrary code into a DTO.
    return code if isinstance(code, str) and len(code) <= 128 else None
