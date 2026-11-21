from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ouro.application.models.requests import ModelRequestPorts, RunModelRequests
from ouro.application.runs.drivers import NativeRunDriverContext
from ouro.application.runs.lifecycle import RunLifecycle
from ouro.core.adapter import RuntimeAdapter
from ouro.core.callbacks import run_blocking
from ouro.core.cancellation import AbortSignal
from ouro.core.inner_loop import InnerAgentLoop
from ouro.core.messages import MessageRecord
from ouro.core.redaction import redact_text
from ouro.core.results import RunResult
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_requests import (
    DriverToolBatchRequest,
    ModelToolBatchRequest,
    ToolBatchHandlingResult,
)
from ouro.core.turn_policies import TurnPolicyEvaluation
from ouro.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from ouro.application.runtime.extensions import RuntimeExtensions
    from ouro.application.tool_calls import ToolCallCoordinator


@dataclass
class RunDriverExecution:
    """Invoke a Driver with one Run's model stream, tool selection and recovery position."""

    runs: RunLifecycle
    model_ports: ModelRequestPorts
    budget_for_run: Callable[[RunRecord], RunBudgetPolicy]
    tools_for_run: Callable[[str], ToolRegistry]
    tool_calls_for_run: Callable[[str], ToolCallCoordinator]
    refresh_configuration: Callable[[], Awaitable[None]]
    consume_inputs: Callable[[RunRecord, str], Awaitable[list[MessageRecord]]]
    evaluate_completion: Callable[..., Awaitable[TurnPolicyEvaluation]]
    default_workspace: str | None

    async def execute(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        adapter: RuntimeAdapter,
        abort_signal: AbortSignal,
        extensions: RuntimeExtensions | None,
        on_provider_stream_start: Callable[[], None] | None = None,
        resuming_driver: bool = False,
        resume_turn_id: str | None = None,
    ) -> RunResult:
        try:
            model_requests = RunModelRequests(
                adapter=adapter,
                tool_definitions=lambda: self.tools_for_run(run.run_id).definitions(),
                run_budget=self.budget_for_run(run),
                ports=self.model_ports,
                on_stream_start=on_provider_stream_start,
            )
            context = NativeRunDriverContext(
                session=session,
                run=run,
                abort_signal=abort_signal,
                request_model=model_requests.execute,
                request_tools=self.request_model_tools,
                request_driver_tools=self.request_driver_tools,
                consume_inputs=self.consume_inputs,
                runs=self.runs,
                workspace=session.workspace or self.default_workspace,
                resume_turn_id=resume_turn_id,
            )
            if run.driver.is_default:
                driver = InnerAgentLoop(turns=NativeRunTurns(context, self.evaluate_completion))
                scope = (
                    extensions.driver_scope(context) if extensions is not None else nullcontext()
                )
                with scope:
                    result = await context.invoke(driver, resuming=resuming_driver)
            else:
                if extensions is None:
                    raise ValueError("Run driver catalog is unavailable")
                async with extensions.bind_driver(run.driver) as binding:
                    result = await context.invoke(binding.value.handler, resuming=resuming_driver)
        except Exception as exc:  # noqa: BLE001 - settle Run driver initialization failures.
            result = await self.runs.fail(
                session=session,
                run=run,
                error=(
                    f"Run driver initialization failed: {type(exc).__name__}: "
                    f"{redact_text(str(exc))}"
                ),
                final_text="",
                had_tool_error=False,
            )
        return await self.runs.finalize_requested_abort(result)

    async def request_driver_tools(
        self, request: DriverToolBatchRequest
    ) -> ToolBatchHandlingResult:
        await self.refresh_configuration()
        run = request.run
        bind = self.model_ports.begin_model_round
        if bind is not None:
            run = await bind(session=request.session, run=run, model_round=run.model_round_count)
        session = await run_blocking(self.runs.store.load_session_record, run.session_id)
        return await self.tool_calls_for_run(run.run_id).handle_driver_batch(
            session=session,
            run=run,
            tool_calls=request.calls,
            turn_id=request.turn_id,
            abort_signal=request.abort_signal,
            checkpoint=request.checkpoint,
        )

    async def request_model_tools(self, request: ModelToolBatchRequest) -> ToolBatchHandlingResult:
        await self.refresh_configuration()
        session = await run_blocking(
            self.runs.store.load_session_record, request.session.session_id
        )
        response = request.response
        return await self.tool_calls_for_run(request.run.run_id).handle_batch(
            session,
            request.run,
            list(response.tool_calls),
            turn_id=request.turn_id,
            assistant_text=response.text,
            assistant_reasoning=response.reasoning,
            assistant_model_response=response.model_response,
            assistant_continuation=response.continuation,
            abort_signal=request.abort_signal,
            driver_checkpoint=request.driver_checkpoint,
        )


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
