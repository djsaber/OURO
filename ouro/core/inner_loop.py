from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ouro.core.run_drivers import DriverFinished, DriverResume, DriverSuspended, RunDriverContext


@dataclass(frozen=True)
class TurnCompletionPolicyResult:
    sequence: int
    should_continue: bool = False
    blocked_reason: str | None = None


class DefaultRunTurns(Protocol):
    async def consume_inputs(self) -> bool: ...

    async def may_finish(self, response_id: str) -> bool: ...


class InnerAgentLoop:
    """The default Driver algorithm; native ports own execution and settlement."""

    def __init__(self, *, turns: DefaultRunTurns) -> None:
        self.turns = turns

    async def __call__(
        self, context: RunDriverContext, resume: DriverResume | None
    ) -> DriverFinished | DriverSuspended:
        if resume is not None:
            await self.turns.consume_inputs()
        while True:
            response = await context.request_model()
            if response.tool_calls:
                result = await context.execute_model_tools(response.response_id, checkpoint="turn")
                if result.suspended:
                    return DriverSuspended()
                await self.turns.consume_inputs()
                continue
            if await self.turns.consume_inputs():
                continue
            if await self.turns.may_finish(response.response_id):
                return DriverFinished(response.text, response_id=response.response_id)
