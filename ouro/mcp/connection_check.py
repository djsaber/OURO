from __future__ import annotations

from ouro.core.base import OuroModel
from ouro.mcp.client import CredentialResolver, MCPClientFactory
from ouro.mcp.manager import MCPManager
from ouro.mcp.models import (
    MCPDiagnostic,
    MCPServerConfig,
    MCPServerInfo,
    MCPServerStatus,
)
from ouro.tools import ToolRegistry


class MCPConnectionCheckResult(OuroModel):
    ok: bool
    status: MCPServerStatus
    server_info: MCPServerInfo | None = None
    tool_names: tuple[str, ...] = ()
    error: str | None = None
    diagnostics: tuple[MCPDiagnostic, ...] = ()


async def check_mcp_server_connection(
    config: MCPServerConfig,
    *,
    client_factory: MCPClientFactory | None = None,
    resolve_credential: CredentialResolver | None = None,
) -> MCPConnectionCheckResult:
    """Perform one isolated handshake and tool discovery without mutating config."""

    check_config = MCPServerConfig.model_validate(
        {
            **config.model_dump(mode="python"),
            "enabled": True,
            "auto_start": False,
            "auto_reconnect": False,
            "max_reconnect_attempts": 0,
        }
    )
    registry = ToolRegistry()
    manager = MCPManager(
        registry=registry,
        client_factory=client_factory,
        resolve_credential=resolve_credential,
    )
    manager.configure(check_config)
    try:
        record = await manager.start(config.server_id)
        return MCPConnectionCheckResult(
            ok=record.status == "connected",
            status=record.status,
            server_info=record.server_info,
            tool_names=tuple(
                str(registry.get(name).definition.metadata["mcp_tool_name"])
                for name in record.tool_names
            ),
            error=record.error,
            diagnostics=tuple(manager.diagnostics(config.server_id)),
        )
    finally:
        await manager.close()
