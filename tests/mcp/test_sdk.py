from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anyio
import pytest
from mcp import types
from mcp.shared.exceptions import McpError

from ouro.core.content import AudioContent, ImageContent, TextContent
from ouro.core.tools import ToolInvocation
from ouro.mcp import (
    MCPDisconnectedError,
    MCPProtocolError,
    MCPServerConfig,
    MCPTimeoutError,
    SDKMCPClientFactory,
)
from ouro.mcp.sdk import SDKMCPClientSession, _remote_result
from ouro.mcp.tool_adapter import mcp_tool_result


def test_mcp_content_preserves_order_and_media_without_stringifying_pixels():
    remote = _remote_result(
        types.CallToolResult(
            content=[
                types.TextContent(type="text", text="before"),
                types.ImageContent(type="image", data="AAAA", mimeType="image/png"),
                types.AudioContent(type="audio", data="YXVkaW8=", mimeType="audio/wav"),
                types.TextContent(type="text", text="after"),
            ],
            structuredContent={"width": 1},
        )
    )
    result = mcp_tool_result(ToolInvocation(tool_name="remote", session_id="s", run_id="r"), remote)
    assert result.content_blocks == [
        TextContent(text="before"),
        ImageContent(uri="data:image/png;base64,AAAA", media_type="image/png"),
        AudioContent(uri="data:audio/wav;base64,YXVkaW8=", media_type="audio/wav"),
        TextContent(text="after"),
    ]
    assert result.text_content == "beforeafter"
    assert result.structured == {"width": 1}


@pytest.mark.parametrize(
    ("code", "message", "kind"),
    [
        (-32602, "disconnected or timeout is not a valid argument", MCPProtocolError),
        (types.CONNECTION_CLOSED, "Connection closed", MCPDisconnectedError),
        (408, "Timed out while waiting for response", MCPTimeoutError),
    ],
)
async def test_sdk_tool_call_classifies_response_by_protocol_code(code, message, kind):
    class Session:
        async def call_tool(self, name, arguments):
            raise McpError(types.ErrorData(code=code, message=message))

    with pytest.raises(kind):
        await SDKMCPClientSession(Session()).call_tool("read", {})


@pytest.mark.asyncio
async def test_sse_adapter_owns_liveness_and_reports_reader_disconnect(monkeypatch) -> None:
    read_writer, read_stream = anyio.create_memory_object_stream(1)
    write_stream, write_reader = anyio.create_memory_object_stream(1)
    observed_clients: list[object] = []
    observed_timeouts: list[float] = []

    @asynccontextmanager
    async def fake_sse_transport(*_args, **kwargs) -> AsyncGenerator[tuple[object, object]]:
        observed_clients.append(kwargs["client"])
        observed_timeouts.append(kwargs["timeout_seconds"])
        yield read_stream, write_stream

    monkeypatch.setattr("ouro.mcp.sdk.legacy_sse_transport", fake_sse_transport)
    connection_errors: list[str] = []
    factory = SDKMCPClientFactory()
    config = MCPServerConfig(
        server_id="remote",
        name="Remote",
        transport="sse",
        url="https://mcp.example.test/sse",
    )

    async with factory.open(
        config,
        resolve_credential=lambda _reference: None,
        diagnostic=lambda _level, _message: None,
        tools_changed=lambda: None,
        connection_lost=connection_errors.append,
    ):
        await read_writer.send(ConnectionError("incomplete chunked read"))
        for _ in range(100):
            if connection_errors:
                break
            await asyncio.sleep(0.001)

    await read_writer.aclose()
    await write_reader.aclose()
    assert len(observed_clients) == 1
    assert observed_timeouts == [30.0]
    assert connection_errors == ["MCP session transport failed: incomplete chunked read"]
