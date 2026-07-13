from __future__ import annotations

import asyncio
from pathlib import Path

from ouro.mcp.configuration import MCPConfigStore
from ouro.mcp.manager import MCPManager
from ouro.mcp.models import MCPServerRecord
from ouro.tools import ToolRegistry


class MCPRuntimeResource:
    """Adapt MCPManager to RuntimeSupervisor's process-resource lifecycle."""

    def __init__(self, manager: MCPManager) -> None:
        self.manager = manager
        self.startup_records: tuple[MCPServerRecord, ...] = ()

    @classmethod
    def from_config(
        cls,
        root: str | Path,
        registry: ToolRegistry,
    ) -> MCPRuntimeResource:
        manager = MCPManager(registry=registry)
        manager.configure_many(MCPConfigStore(root).snapshot().servers)
        return cls(manager)

    async def __aenter__(self) -> MCPRuntimeResource:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def start(self) -> None:
        try:
            self.startup_records = tuple(await self.manager.start_auto())
        except asyncio.CancelledError:
            await asyncio.shield(self.manager.close())
            raise
        except Exception:
            await self.manager.close()
            raise

    async def close(self) -> None:
        await self.manager.close()

    @property
    def startup_failures(self) -> tuple[MCPServerRecord, ...]:
        return tuple(record for record in self.startup_records if record.status == "failed")
