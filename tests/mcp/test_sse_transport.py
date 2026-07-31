from __future__ import annotations

import anyio
import httpx
import pytest
from mcp import types
from mcp.shared.message import SessionMessage

from ouro.mcp.sse_transport import legacy_sse_transport


class _OpenSSEStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = anyio.Event()

    async def __aiter__(self):
        yield b"event: endpoint\ndata: /message?sessionId=expired\n\n"
        await self.closed.wait()

    async def aclose(self) -> None:
        self.closed.set()


@pytest.mark.asyncio
async def test_post_failure_enters_session_stream_without_logging_traceback(caplog) -> None:
    event_stream = _OpenSSEStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=event_stream,
            )
        return httpx.Response(400, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(1, read=None),
    ) as client:
        async with legacy_sse_transport(
            "https://mcp.example.test/sse",
            client=client,
            timeout_seconds=1,
        ) as (read_stream, write_stream):
            request = types.JSONRPCRequest(
                jsonrpc="2.0",
                id=1,
                method="ping",
            )
            await write_stream.send(SessionMessage(types.JSONRPCMessage(request)))
            failure = await read_stream.receive()

    assert isinstance(failure, httpx.HTTPStatusError)
    assert failure.response.status_code == 400
    assert caplog.records == []


@pytest.mark.asyncio
async def test_sse_message_endpoint_must_match_connection_origin() -> None:
    class CrossOriginStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"event: endpoint\ndata: https://attacker.example/message\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=CrossOriginStream(),
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(1, read=None),
    ) as client:
        with pytest.raises(ExceptionGroup) as captured:
            async with legacy_sse_transport(
                "https://mcp.example.test/sse",
                client=client,
                timeout_seconds=1,
            ):
                pass

    assert "connection origin" in str(captured.value.exceptions[0])
