from __future__ import annotations

from collections.abc import Awaitable, Callable

from ouro.application.run_drivers import NativeRunDriverContext
from ouro.core.inner_loop import TurnCompletionPolicyResult


class NativeRunTurns:
    """Default-loop turn semantics over the same native Driver execution state."""

    def __init__(
        self,
        context: NativeRunDriverContext,
        evaluate_completion: Callable[..., Awaitable[TurnCompletionPolicyResult]],
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
                context.emitted,
                context.sequence,
                session=context.session,
                run=context.run,
                messages=context.messages,
                assistant_text=response.text,
                assistant_reasoning=response.reasoning,
                turn_id=context.turn_id,
            )
        except Exception as exc:  # noqa: BLE001 - native turn policy boundary.
            await context.fail(f"turn policy evaluation failed: {exc}")
        context.sequence = completion.sequence
        if completion.blocked_reason is not None:
            await context.pause("turn_policy_blocked")
        context.messages.extend(completion.messages)
        if completion.should_continue:
            context.final_text = ""
        return not completion.should_continue
