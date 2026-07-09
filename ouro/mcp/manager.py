from __future__ import annotations

import builtins
from typing import Literal

from pydantic import Field

from ouro.core.base import Metadata, MetadataField, OuroModel
from ouro.tools import Tool, ToolRegistry

MCPTransport = Literal["stdio", "sse", "http"]
MCPServerStatus = Literal[
    "configured",
    "starting",
    "connected",
    "failed",
    "stopping",
    "stopped",
    "disabled",
]


class MCPServerConfig(OuroModel):
    server_id: str
    name: str
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    transport: MCPTransport = "stdio"
    credential_refs: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    auto_start: bool = False
    timeout_seconds: float = 30.0
    metadata: Metadata = MetadataField


class MCPServerRecord(OuroModel):
    config: MCPServerConfig
    status: MCPServerStatus = "configured"
    error: str | None = None
    metadata: Metadata = MetadataField


class MCPManager:
    def __init__(self) -> None:
        self._servers: dict[str, MCPServerRecord] = {}

    def configure(self, config: MCPServerConfig) -> MCPServerRecord:
        status: MCPServerStatus = "configured" if config.enabled else "disabled"
        record = MCPServerRecord(config=config, status=status)
        self._servers[config.server_id] = record
        return record

    def get(self, server_id: str) -> MCPServerRecord:
        try:
            return self._servers[server_id]
        except KeyError as exc:
            raise KeyError(f"unknown MCP server: {server_id}") from exc

    def list(self) -> builtins.list[MCPServerRecord]:
        return [self._servers[server_id] for server_id in sorted(self._servers)]

    async def start(self, server_id: str) -> MCPServerRecord:
        record = self.get(server_id)
        if not record.config.enabled:
            record = record.model_copy(update={"status": "disabled"})
            self._servers[server_id] = record
            return record
        record = record.model_copy(update={"status": "starting", "error": None})
        self._servers[server_id] = record
        record = record.model_copy(update={"status": "connected"})
        self._servers[server_id] = record
        return record

    async def stop(self, server_id: str) -> MCPServerRecord:
        record = self.get(server_id)
        record = record.model_copy(update={"status": "stopping"})
        self._servers[server_id] = record
        record = record.model_copy(update={"status": "stopped"})
        self._servers[server_id] = record
        return record

    async def restart(self, server_id: str) -> MCPServerRecord:
        await self.stop(server_id)
        return await self.start(server_id)

    def mark_failed(self, server_id: str, error: str) -> MCPServerRecord:
        record = self.get(server_id).model_copy(update={"status": "failed", "error": error})
        self._servers[server_id] = record
        return record

    def register_tools(
        self,
        *,
        server_id: str,
        tools: builtins.list[Tool],
        registry: ToolRegistry,
    ) -> builtins.list[str]:
        self.get(server_id)
        registered: builtins.list[str] = []
        for tool in tools:
            metadata = dict(tool.definition.metadata)
            metadata["mcp_server_id"] = server_id
            definition = tool.definition.model_copy(
                update={
                    "source": "mcp",
                    "metadata": metadata,
                }
            )
            registry.register(Tool(definition=definition, handler=tool.handler))
            registered.append(definition.name)
        return registered
