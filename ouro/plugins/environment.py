from __future__ import annotations

from ouro.core.callbacks import run_blocking
from ouro.plugins.catalog import GenerationGate
from ouro.plugins.instructions import AgentInstructionLoader
from ouro.plugins.invocation import current_invocation
from ouro.sdk.plugins.environment import AgentInstructionDocument


class PluginEnvironmentService:
    """Provide deployment-owned facts without exposing the application's home directory."""

    def __init__(self, loader: AgentInstructionLoader, gate: GenerationGate) -> None:
        self.loader = loader
        self.gate = gate

    async def agent_instructions(
        self, workspace: str | None = None
    ) -> tuple[AgentInstructionDocument, ...]:
        invocation = current_invocation.get()
        if invocation is not None:
            invocation.require_active()
        async with self.gate.invocation():
            return await run_blocking(self.loader.load, workspace)
