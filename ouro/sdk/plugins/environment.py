from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class AgentInstructionDocument:
    path: str
    scope: Literal["user", "project"]
    content: str


class PluginEnvironment(Protocol):
    async def agent_instructions(
        self, workspace: str | None = None
    ) -> tuple[AgentInstructionDocument, ...]: ...
