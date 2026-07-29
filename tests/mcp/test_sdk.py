from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import httpx
import pytest

from ouro.mcp import MCPServerConfig, SDKMCPClientFactory


@pytest.mark.asyncio
async def test_sse_adapter_owns_liveness_and_reports_reader_disconnect(monkeypatch) -> None:
    read_writer, read_stream = anyio.create_memory_object_stream(1)
    write_stream, write_reader = anyio.create_memory_object_stream(1)
    observed_read_timeout: list[float | None] = []

    @asynccontextmanager
    async def fake_sse_client(*_args, **kwargs) -> AsyncIterator[tuple[object, object]]:
        observed_read_timeout.append(kwargs["sse_read_timeout"])
        yield read_stream, write_stream

    monkeypatch.setattr("ouro.mcp.sdk.sse_client", fake_sse_client)
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
        await read_writer.send(httpx.RemoteProtocolError("incomplete chunked read"))
        for _ in range(100):
            if connection_errors:
                break
            await asyncio.sleep(0.001)

    await read_writer.aclose()
    await write_reader.aclose()
    assert observed_read_timeout == [None]
    assert connection_errors == ["MCP session transport failed: incomplete chunked read"]


def test_expected_sse_disconnect_traceback_does_not_escape_sdk_boundary(caplog) -> None:
    logger = logging.getLogger("mcp.client.sse")

    with caplog.at_level(logging.ERROR, logger="mcp.client.sse"):
        try:
            raise httpx.RemoteProtocolError("incomplete chunked read")
        except httpx.RemoteProtocolError:
            logger.exception("Error in sse_reader")

    assert caplog.records == []


def test_unexpected_sse_reader_failure_remains_observable(caplog) -> None:
    logger = logging.getLogger("mcp.client.sse")

    with caplog.at_level(logging.ERROR, logger="mcp.client.sse"):
        try:
            raise ValueError("invalid event")
        except ValueError:
            logger.exception("Error in sse_reader")

    assert [record.getMessage() for record in caplog.records] == ["Error in sse_reader"]
