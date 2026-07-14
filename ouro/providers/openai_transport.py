from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol

import httpx

_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


class OpenAITransportError(RuntimeError):
    """Provider transport failure with an explicit retry classification."""

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


class OpenAICompatibleTransport(Protocol):
    async def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> dict[str, Any]: ...


class HttpxOpenAICompatibleTransport:
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
            response = await self._post(
                url=url,
                headers=request_headers,
                payload=payload,
                timeout=timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text
            status_code = exc.response.status_code
            raise OpenAITransportError(
                f"provider HTTP {status_code}: {_compact_body(body)}",
                retryable=status_code in _RETRYABLE_STATUS_CODES,
                status_code=status_code,
            ) from exc
        except httpx.TimeoutException as exc:
            raise OpenAITransportError(
                "provider request timed out",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise OpenAITransportError(
                f"provider connection failed: {exc}",
                retryable=True,
            ) from exc

        if not response.content:
            return {}
        try:
            parsed = response.json()
        except ValueError as exc:
            raise OpenAITransportError(
                "provider response was not valid JSON",
                retryable=False,
            ) from exc
        if not isinstance(parsed, dict):
            raise OpenAITransportError(
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
        try:
            async with self._client().stream(
                "POST",
                url,
                headers=dict(request_headers),
                json=dict(payload),
                timeout=timeout,
            ) as response:
                async for item in _stream_sse_json(response):
                    yield item
        except httpx.TimeoutException as exc:
            raise OpenAITransportError(
                "provider request timed out",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise OpenAITransportError(
                f"provider connection failed: {exc}",
                retryable=True,
            ) from exc

    async def _post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> httpx.Response:
        return await self._client().post(
            url,
            headers=dict(headers),
            json=dict(payload),
            timeout=timeout,
        )


def _compact_body(body: str) -> str:
    compacted = " ".join(body.split())
    return compacted[:500]


async def _stream_sse_json(response: httpx.Response) -> AsyncIterator[Mapping[str, Any]]:
    if response.status_code >= 400:
        body = (await response.aread()).decode(errors="replace")
        raise OpenAITransportError(
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
        raise OpenAITransportError(
            "provider stream chunk was not valid JSON",
            retryable=False,
        ) from exc
    if not isinstance(parsed, Mapping):
        raise OpenAITransportError(
            "provider stream chunk JSON must be an object",
            retryable=False,
        )
    return parsed
