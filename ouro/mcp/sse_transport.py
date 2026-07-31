from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from urllib.parse import urljoin, urlsplit

import anyio
import httpx
from anyio.abc import TaskStatus
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from httpx_sse import EventSource, aconnect_sse
from mcp import types
from mcp.shared.message import SessionMessage

SSEStreams = tuple[
    MemoryObjectReceiveStream[SessionMessage | Exception],
    MemoryObjectSendStream[SessionMessage],
]


@asynccontextmanager
async def legacy_sse_transport(
    url: str,
    *,
    client: httpx.AsyncClient,
    timeout_seconds: float,
) -> AsyncGenerator[SSEStreams, None]:
    """Expose legacy MCP-over-SSE as SDK-compatible message streams.

    Legacy SSE uses one long-lived GET stream and a session-specific POST
    endpoint. Both directions belong to the same connection: failure in either
    direction is therefore forwarded to the session read stream so OURO can
    recycle the whole connection immediately.
    """

    read_sender, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_receiver = anyio.create_memory_object_stream[SessionMessage](0)

    async with anyio.create_task_group() as task_group:
        try:
            async with aconnect_sse(client, "GET", url) as event_source:
                event_source.response.raise_for_status()
                with anyio.fail_after(timeout_seconds):
                    endpoint_url = await task_group.start(
                        _read_events,
                        event_source,
                        url,
                        read_sender,
                    )
                task_group.start_soon(
                    _write_messages,
                    client,
                    endpoint_url,
                    timeout_seconds,
                    write_receiver,
                    write_stream,
                    read_sender,
                )
                try:
                    yield read_stream, write_stream
                finally:
                    task_group.cancel_scope.cancel()
        finally:
            await read_sender.aclose()
            await read_stream.aclose()
            await write_stream.aclose()
            await write_receiver.aclose()


async def _read_events(
    event_source: EventSource,
    connection_url: str,
    read_sender: MemoryObjectSendStream[SessionMessage | Exception],
    *,
    task_status: TaskStatus[str] = anyio.TASK_STATUS_IGNORED,
) -> None:
    endpoint_received = False
    try:
        async for event in event_source.aiter_sse():
            if event.event == "endpoint":
                endpoint_url = _validated_endpoint(connection_url, event.data)
                if not endpoint_received:
                    endpoint_received = True
                    task_status.started(endpoint_url)
                continue
            if event.event != "message" or not event.data:
                continue
            try:
                message = types.JSONRPCMessage.model_validate_json(event.data)
            except Exception as exc:  # noqa: BLE001 - malformed remote input crosses the boundary.
                await read_sender.send(exc)
                continue
            await read_sender.send(SessionMessage(message))
        if endpoint_received:
            await _send_transport_error(
                read_sender,
                ConnectionError("legacy SSE event stream closed"),
            )
    except Exception as exc:  # noqa: BLE001 - transport failures are session input.
        if not endpoint_received:
            raise
        await _send_transport_error(read_sender, exc)
    finally:
        await read_sender.aclose()


async def _write_messages(
    client: httpx.AsyncClient,
    endpoint_url: str,
    timeout_seconds: float,
    write_receiver: MemoryObjectReceiveStream[SessionMessage],
    write_stream: MemoryObjectSendStream[SessionMessage],
    read_sender: MemoryObjectSendStream[SessionMessage | Exception],
) -> None:
    try:
        async with write_receiver:
            async for session_message in write_receiver:
                response = await client.post(
                    endpoint_url,
                    timeout=timeout_seconds,
                    json=session_message.message.model_dump(
                        by_alias=True,
                        mode="json",
                        exclude_none=True,
                    ),
                )
                response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - transport failures are session input.
        await _send_transport_error(read_sender, exc)
    finally:
        await write_stream.aclose()


def _validated_endpoint(connection_url: str, endpoint: str) -> str:
    endpoint_url = urljoin(connection_url, endpoint)
    parsed_endpoint = urlsplit(endpoint_url)
    if parsed_endpoint.username is not None or parsed_endpoint.password is not None:
        raise ValueError("legacy SSE message endpoint must not contain credentials")
    connection_origin = _origin(connection_url)
    endpoint_origin = _origin(endpoint_url)
    if connection_origin != endpoint_origin:
        raise ValueError("legacy SSE message endpoint must use the connection origin")
    return endpoint_url


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname or "", parsed.port


async def _send_transport_error(
    read_sender: MemoryObjectSendStream[SessionMessage | Exception],
    error: Exception,
) -> None:
    with suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
        await read_sender.send(error)
