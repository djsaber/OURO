from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anyio
import pytest

from ouro.mcp import MCPServerConfig, SDKMCPClientFactory


@pytest.mark.asyncio
async def test_sse_adapter_owns_liveness_and_reports_reader_disconnect(monkeypatch) -> None:
    read_writer, read_stream = anyio.create_memory_object_stream(1)
    write_stream, write_reader = anyio.create_memory_object_stream(1)
    observed_clients: list[object] = []
    observed_timeouts: list[float] = []

    @asynccontextmanager
    async def fake_sse_transport(*_args, **kwargs) -> AsyncGenerator[tuple[object, object], None]:
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
