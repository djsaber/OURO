from __future__ import annotations

import pytest

from ouro.mcp import MCPManager, MCPServerConfig
from ouro.tools import Tool, ToolDefinition, ToolInvocation, ToolRegistry, ToolResult


@pytest.mark.asyncio
async def test_mcp_manager_tracks_lifecycle() -> None:
    manager = MCPManager()
    manager.configure(MCPServerConfig(server_id="fs", name="Filesystem"))
    assert manager.list()[0].config.server_id == "fs"

    connected = await manager.start("fs")
    assert connected.status == "connected"

    stopped = await manager.stop("fs")
    assert stopped.status == "stopped"

    restarted = await manager.restart("fs")
    assert restarted.status == "connected"


def test_mcp_manager_registers_tools_with_server_metadata() -> None:
    manager = MCPManager()
    manager.configure(MCPServerConfig(server_id="fs", name="Filesystem"))
    registry = ToolRegistry()

    def handler(invocation: ToolInvocation) -> ToolResult:
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="ok",
        )

    registered = manager.register_tools(
        server_id="fs",
        registry=registry,
        tools=[
            Tool(
                definition=ToolDefinition(name="mcp_read", description="Read through MCP."),
                handler=handler,
            )
        ],
    )

    definition = registry.definitions()[0]
    assert registered == ["mcp_read"]
    assert definition.source == "mcp"
    assert definition.metadata["mcp_server_id"] == "fs"
