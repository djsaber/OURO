from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import pytest

from ouro.mcp import MCPManager, MCPServerConfig
from ouro.tools import ToolInvocation, ToolRegistry

SERVER_SCRIPT = Path(__file__).parent / "fixtures" / "echo_server.py"


@pytest.mark.asyncio
async def test_stdio_server_full_lifecycle() -> None:
    registry = ToolRegistry()
    manager = MCPManager(registry=registry)
    manager.configure(
        MCPServerConfig(
            server_id="stdio",
            name="Stdio integration",
            command=sys.executable,
            args=[str(SERVER_SCRIPT), "--transport", "stdio"],
            timeout_seconds=5,
        )
    )

    connected = await manager.start("stdio")
    assert connected.status == "connected", connected.error
    assert registry.names() == ["stdio__echo"]

    result = await registry.get("stdio__echo").execute(
        ToolInvocation(
            session_id="session",
            run_id="run",
            tool_name="stdio__echo",
            arguments={"value": "hello"},
        )
    )
    assert result.is_error is False
    assert result.structured == {"value": "hello"}

    await manager.close()
    assert manager.get("stdio").status == "stopped"
    assert registry.names() == []


@pytest.mark.asyncio
async def test_streamable_http_server_full_lifecycle() -> None:
    port = _unused_tcp_port()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SERVER_SCRIPT),
        "--transport",
        "http",
        "--port",
        str(port),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    registry = ToolRegistry()
    manager = MCPManager(registry=registry)
    manager.configure(
        MCPServerConfig(
            server_id="http",
            name="HTTP integration",
            transport="http",
            url=f"http://127.0.0.1:{port}/mcp",
            timeout_seconds=2,
            max_reconnect_attempts=10,
            reconnect_backoff_seconds=0.05,
        )
    )
    try:
        connected = await manager.start("http")
        assert connected.status == "connected", connected.error
        result = await registry.get("http__echo").execute(
            ToolInvocation(
                session_id="session",
                run_id="run",
                tool_name="http__echo",
                arguments={"value": "hello"},
            )
        )
        assert result.structured == {"value": "hello"}
        await manager.close()
        assert registry.names() == []
    finally:
        if manager.get("http").status != "stopped":
            await manager.close()
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)


@pytest.mark.asyncio
async def test_legacy_sse_server_remains_usable_while_interactive_host_is_idle() -> None:
    port = _unused_tcp_port()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SERVER_SCRIPT),
        "--transport",
        "sse",
        "--port",
        str(port),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    registry = ToolRegistry()
    manager = MCPManager(registry=registry)
    manager.configure(
        MCPServerConfig(
            server_id="sse",
            name="Legacy SSE integration",
            transport="sse",
            url=f"http://127.0.0.1:{port}/sse",
            timeout_seconds=0.2,
            health_check_interval_seconds=1,
            max_reconnect_attempts=10,
            reconnect_backoff_seconds=0.05,
        )
    )
    try:
        connected = await manager.start("sse")
        assert connected.status == "connected", connected.error

        # Request timeouts must not become passive read deadlines for the
        # long-lived SSE stream while the interactive host is awaiting input.
        await asyncio.sleep(0.4)

        result = await registry.get("sse__echo").execute(
            ToolInvocation(
                session_id="session",
                run_id="run",
                tool_name="sse__echo",
                arguments={"value": "still connected"},
            )
        )
        assert result.structured == {"value": "still connected"}
    finally:
        await manager.close()
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)


def _unused_tcp_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
