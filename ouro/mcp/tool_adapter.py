from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from ouro.core.base import utc_now
from ouro.mcp.models import MCPRemoteTool, MCPRemoteToolResult, MCPServerConfig
from ouro.tools import (
    Tool,
    ToolDefinition,
    ToolEffect,
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
    effect, effect_source = _resolve_effect(config, remote)
    definition = ToolDefinition(
        name=name,
        display_name=f"{config.name}: {remote.title or remote.name}",
        description=remote.description or f"MCP tool {remote.name}",
        parameters_schema=remote.input_schema,
        result_schema=remote.output_schema,
        source="mcp",
        permission_mode=config.permission_mode,
        effect=effect,
        metadata={
            "mcp_server_id": config.server_id,
            "mcp_tool_name": remote.name,
            "mcp_annotations": dict(remote.annotations),
            "effect_source": effect_source,
        },
    )
    validate_tool_definition(definition)

    async def handler(
        invocation: ToolInvocation,
        context: ToolExecutionContext,
    ) -> ToolResult:
        return await invoke(config.server_id, remote.name, invocation, context)

    return Tool(definition=definition, handler=handler)


def _resolve_effect(
    config: MCPServerConfig,
    remote: MCPRemoteTool,
) -> tuple[ToolEffect, str]:
    override = config.tool_effect_overrides.get(remote.name)
    if override is not None:
        return override, "user_override"
    read_only = remote.annotations.get("readOnlyHint")
    if read_only is True:
        return "read_only", "mcp_annotation"
    if read_only is False:
        return "state_changing", "mcp_annotation"
    return "unknown", "default"


def exposed_tool_name(server_id: str, remote_name: str) -> str:
    if not remote_name or not _MCP_TOOL_NAME.fullmatch(remote_name):
        raise ValueError(f"invalid MCP tool name: {remote_name!r}")
    name = f"{server_id}__{remote_name}"
    if len(name) > 128:
        raise ValueError(f"MCP tool name is too long: {remote_name!r}")
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
