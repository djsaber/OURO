from __future__ import annotations

from .environment import AgentInstructionDocument
from .worker import WorkerPeer


class WorkerEnvironment:
    def __init__(self, peer: WorkerPeer) -> None:
        self.peer = peer

    async def agent_instructions(
        self, workspace: str | None = None
    ) -> tuple[AgentInstructionDocument, ...]:
        result = await self.peer.request("environment.agent_instructions", {"workspace": workspace})
        return tuple(AgentInstructionDocument(**item) for item in result)
