from __future__ import annotations

from collections.abc import Awaitable, Callable

from ouro.application.runs.drivers import NativeRunDriverContext
from ouro.core.turn_policies import TurnPolicyEvaluation


class NativeRunTurns:
    """Default-loop turn semantics over the same native Driver execution state."""

    def __init__(
        self,
        context: NativeRunDriverContext,
        evaluate_completion: Callable[..., Awaitable[TurnPolicyEvaluation]],
    ) -> None:
        self.context = context
        self.evaluate_completion = evaluate_completion

    async def consume_inputs(self) -> bool:
        return bool(await self.context.consume_inputs())

    async def may_finish(self, response_id: str) -> bool:
        context = self.context
        response = context.model_response(response_id)
        try:
            completion = await self.evaluate_completion(
                session=context.session,
                run=context.run,
                assistant_text=response.text,
                assistant_reasoning=response.reasoning,
                turn_id=context.turn_id,
            )
        except Exception as exc:  # noqa: BLE001 - native turn policy boundary.
            await context.fail(f"turn policy evaluation failed: {exc}")
        if completion.failures:
            await context.pause("turn_policy_blocked")
        if completion.should_continue:
            context.final_text = ""
            return False
        return True
