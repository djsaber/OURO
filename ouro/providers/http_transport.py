from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol

import httpx

_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


class ProviderTransportError(RuntimeError):
    """HTTP provider failure with an explicit retry classification."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


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
    """Shared JSON and SSE transport for HTTP-based provider protocols."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client
        self._owns_client = client is None

    def _client(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient()
        return self.client

    async def aclose(self) -> None:
        if self._owns_client and self.client is not None:
            await self.client.aclose()
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
        try:
            response = await self._client().post(
                url,
                headers=dict(request_headers),
                json=dict(payload),
                timeout=timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            raise ProviderTransportError(
                f"provider HTTP {status_code}: {_compact_body(exc.response.text)}",
                retryable=status_code in _RETRYABLE_STATUS_CODES,
                status_code=status_code,
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderTransportError(
                "provider request timed out",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderTransportError(
                f"provider connection failed: {_describe_request_error(exc)}",
                retryable=True,
            ) from exc

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
        try:
            async with self._client().stream(
                "POST",
                url,
                headers=dict(request_headers),
                json=dict(payload),
                timeout=stream_timeout,
            ) as response:
                async for item in _stream_sse_json(response):
                    yield item
        except httpx.TimeoutException as exc:
            raise ProviderTransportError(
                "provider request timed out",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderTransportError(
                f"provider connection failed: {_describe_request_error(exc)}",
                retryable=True,
            ) from exc


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


async def _stream_sse_json(response: httpx.Response) -> AsyncIterator[Mapping[str, Any]]:
    if response.status_code >= 400:
        body = (await response.aread()).decode(errors="replace")
        raise ProviderTransportError(
            f"provider HTTP {response.status_code}: {_compact_body(body)}",
            retryable=response.status_code in _RETRYABLE_STATUS_CODES,
            status_code=response.status_code,
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
