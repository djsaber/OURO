from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from ouro.core.base import utc_now
from ouro.mcp.models import MCPRemoteTool, MCPRemoteToolResult, MCPServerConfig
from ouro.tools import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolInvocation,
    ToolResult,
)
from ouro.tools.schema import validate_tool_definition

MCPToolInvoker = Callable[[str, str, ToolInvocation, ToolExecutionContext], Awaitable[ToolResult]]
_MCP_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def adapt_mcp_tool(
    config: MCPServerConfig,
    remote: MCPRemoteTool,
    invoke: MCPToolInvoker,
) -> Tool:
    name = exposed_tool_name(config.server_id, remote.name)
    definition = ToolDefinition(
        name=name,
        display_name=remote.title,
        description=remote.description or f"MCP tool {remote.name}",
        parameters_schema=remote.input_schema,
        result_schema=remote.output_schema,
        source="mcp",
        permission_mode=config.permission_mode,
        metadata={
            "mcp_server_id": config.server_id,
            "mcp_tool_name": remote.name,
            "mcp_annotations": dict(remote.annotations),
        },
    )
    validate_tool_definition(definition)

    async def handler(
        invocation: ToolInvocation,
        context: ToolExecutionContext,
    ) -> ToolResult:
        return await invoke(config.server_id, remote.name, invocation, context)

    return Tool(definition=definition, handler=handler)


def exposed_tool_name(server_id: str, remote_name: str) -> str:
    if not remote_name or not _MCP_TOOL_NAME.fullmatch(remote_name):
        raise ValueError(f"invalid MCP tool name from {server_id}: {remote_name!r}")
    name = f"{server_id}__{remote_name}"
    if len(name) > 128:
        raise ValueError(f"MCP tool identity is too long: {name}")
    return name


def mcp_tool_result(
    invocation: ToolInvocation,
    remote: MCPRemoteToolResult,
) -> ToolResult:
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        is_error=remote.is_error,
        content=remote.content,
        structured=remote.structured,
        ended_at=utc_now(),
        metadata=remote.metadata,
    )
