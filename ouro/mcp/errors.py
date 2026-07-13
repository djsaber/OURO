from __future__ import annotations


class MCPError(Exception):
    """Base error exposed by OURO's MCP boundary."""


class MCPConfigurationError(MCPError):
    pass


class MCPStateError(MCPError):
    pass


class MCPConnectionError(MCPError):
    pass


class MCPProtocolError(MCPError):
    pass


class MCPTimeoutError(MCPError):
    pass


class MCPDisconnectedError(MCPConnectionError):
    pass


class MCPToolCallError(MCPError):
    pass
