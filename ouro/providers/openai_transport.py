from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol

import httpx


class OpenAICompatibleTransport(Protocol):
    async def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> dict[str, Any]: ...


class OpenAICompatibleStreamingTransport(OpenAICompatibleTransport, Protocol):
    def stream_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> AsyncIterator[Mapping[str, Any]]: ...


class HttpxOpenAICompatibleTransport:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

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
            raise RuntimeError(
                f"provider HTTP {exc.response.status_code}: {_compact_body(body)}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError("provider request timed out") from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"provider connection failed: {exc}") from exc

        if not response.content:
            return {}
        try:
            parsed = response.json()
        except ValueError as exc:
            raise RuntimeError("provider response was not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("provider response JSON must be an object")
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
            if self.client is not None:
                async with self.client.stream(
                    "POST",
                    url,
                    headers=dict(request_headers),
                    json=dict(payload),
                    timeout=timeout,
                ) as response:
                    async for item in _stream_sse_json(response):
                        yield item
                return

            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=dict(request_headers),
                    json=dict(payload),
                    timeout=timeout,
                ) as response:
                    async for item in _stream_sse_json(response):
                        yield item
        except httpx.TimeoutException as exc:
            raise RuntimeError("provider request timed out") from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"provider connection failed: {exc}") from exc

    async def _post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> httpx.Response:
        if self.client is not None:
            return await self.client.post(
                url,
                headers=dict(headers),
                json=dict(payload),
                timeout=timeout,
            )
        async with httpx.AsyncClient() as client:
            return await client.post(
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
        raise RuntimeError(f"provider HTTP {response.status_code}: {_compact_body(body)}")

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
        raise RuntimeError("provider stream chunk was not valid JSON") from exc
    if not isinstance(parsed, Mapping):
        raise RuntimeError("provider stream chunk JSON must be an object")
    return parsed
