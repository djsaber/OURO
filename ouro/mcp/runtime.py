from __future__ import annotations

from ouro.mcp.manager import MCPManager


class MCPRuntimeResource:
    """Adapt MCPManager to RuntimeSupervisor's process-resource lifecycle."""

    def __init__(self, manager: MCPManager) -> None:
        self.manager = manager

    async def start(self) -> None:
        await self.manager.start_auto()

    async def close(self) -> None:
        await self.manager.close()
