from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from ouro.mcp.models import (
    MCPRemoteTool,
    MCPRemoteToolResult,
    MCPServerConfig,
    MCPServerInfo,
)

MCPDiagnosticHandler = Callable[[str, str], None]
MCPToolsChangedHandler = Callable[[], None]
MCPConnectionLostHandler = Callable[[str], None]
CredentialResolver = Callable[[str], str | None]


class MCPClientSession(Protocol):
    async def initialize(self) -> MCPServerInfo: ...

    async def list_tools(
        self, cursor: str | None = None
    ) -> tuple[list[MCPRemoteTool], str | None]: ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> MCPRemoteToolResult: ...

    async def ping(self) -> None: ...


class MCPClientFactory(Protocol):
    def open(
        self,
        config: MCPServerConfig,
        *,
        resolve_credential: CredentialResolver,
        diagnostic: MCPDiagnosticHandler,
        tools_changed: MCPToolsChangedHandler,
        connection_lost: MCPConnectionLostHandler,
    ) -> AbstractAsyncContextManager[MCPClientSession]: ...


AsyncCallback = Callable[[], Awaitable[None]]
