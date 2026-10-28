from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Never

from pydantic import JsonValue

from ouro.application.runs.lifecycle import RunLifecycle
from ouro.core.base import new_id
from ouro.core.callbacks import run_blocking
from ouro.core.cancellation import AbortSignal
from ouro.core.messages import MessageRecord
from ouro.core.model_execution import ModelExecutionResult
from ouro.core.model_requests import (
    ModelRequestBlocked,
    ModelRequestFailed,
    RunModelRequest,
    RunModelRequester,
)
from ouro.core.redaction import redact_text
from ouro.core.results import RunResult
from ouro.core.run_budget import RunPauseReason
from ouro.core.run_drivers import (
    DriverCheckpoint,
    DriverFinished,
    DriverInputConsumer,
    DriverModelResponse,
    DriverResume,
    DriverSuspended,
    DriverToolResult,
    RunDriverHandler,
)
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_requests import (
    DriverToolBatchRequest,
    DriverToolBatchRequester,
    ModelToolBatchRequest,
    ModelToolBatchRequester,
    ToolBatchHandlingResult,
)
from ouro.core.tools import ToolCallRequest


class _DriverHalted(Exception):
    """The host has already settled a blocked native operation."""


class NativeRunDriverContext:
    """Own sequential native operations and response handles for one Driver invocation."""

    def __init__(
        self,
        *,
        session: SessionRecord,
        run: RunRecord,
        sequence: int,
        abort_signal: AbortSignal,
        request_model: RunModelRequester,
        request_tools: ModelToolBatchRequester,
        request_driver_tools: DriverToolBatchRequester,
        consume_inputs: DriverInputConsumer,
        runs: RunLifecycle,
        workspace: str | None = None,
        workspace_only: bool = False,
        resume_turn_id: str | None = None,
    ) -> None:
        self.session, self.run = session, run
        self.sequence = sequence
        self.signal = abort_signal
        self.workspace = workspace or session.workspace
        self.workspace_only = workspace_only
        self._request_model, self._request_tools, self._runs = request_model, request_tools, runs
        self._request_driver_tools = request_driver_tools
        self._consume_inputs = consume_inputs
        self._inputs_checked = False
        self._active = True
        self._busy = False
        self._operation_idle = asyncio.Event()
        self._operation_idle.set()
        self._operation_task: asyncio.Task[object] | None = None
        self._pending: tuple[str, str, ModelExecutionResult] | None = None
        self._response: tuple[str, ModelExecutionResult] | None = None
        self.turn_id = (
            run.active_tool_batch.turn_id if run.active_tool_batch is not None else resume_turn_id
        )
        self.final_text = ""
        self.suspended = False
        self.stopped: RunResult | None = None
        self.had_tool_error = False

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def run_id(self) -> str:
        return self.run.run_id

    @property
    def is_aborted(self) -> bool:
        return self.signal.is_aborted()

    def close(self) -> None:
        self._active = False
        self._pending = None

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        if not self._active or self.suspended or self.stopped is not None:
            raise RuntimeError("Driver invocation no longer accepts native operations")
        if self._busy:
            raise RuntimeError("native operations on one Run must be awaited sequentially")
        if self.is_aborted:
            raise asyncio.CancelledError
        self._busy = True
        self._operation_idle.clear()
        self._operation_task = asyncio.current_task()
        try:
            yield
        finally:
            self._busy = False
            self._operation_task = None
            self._operation_idle.set()

    async def request_model(
        self, *, prompt: str = "", tools: tuple[str, ...] | None = None
    ) -> DriverModelResponse:
        async with self._operation():
            if self._pending is not None:
                raise ValueError(
                    "resolve the pending model tool response before requesting another model"
                )
            await self._deliver_inputs()
            turn_id = new_id("turn")
            self.turn_id = turn_id
            self._response = None
            self.final_text = ""
            result = await self._request_model(
                RunModelRequest(
                    session=self.session,
                    run=self.run,
                    model_round=self.run.model_round_count + 1,
                    turn_id=turn_id,
                    sequence=self.sequence,
                    abort_signal=self.signal,
                    prompt=prompt,
                    tools=tools,
                )
            )
            self.session, self.run, self.sequence = result.session, result.run, result.sequence
            response = result.outcome
            if isinstance(response, ModelRequestBlocked):
                await self.pause(response.reason)
            if isinstance(response, ModelRequestFailed):
                await self.fail(response.error)
            assert isinstance(response, ModelExecutionResult)
            self.final_text = response.text
            self._inputs_checked = False
            if response.aborted or self.is_aborted:
                raise asyncio.CancelledError
            if response.error:
                await self.fail(response.error, response=response)
            response_id = new_id("response")
            self._response = (response_id, response)
            if response.tool_calls:
                self._pending = (response_id, turn_id, response)
            else:
                await run_blocking(
                    self._runs.append_assistant_message,
                    session=self.session,
                    run=self.run,
                    text=response.text,
                    reasoning_content=response.reasoning,
                    model_response=response.model_response,
                    provider_continuation=response.continuation,
                    turn_id=turn_id,
                )
            return DriverModelResponse(
                response_id,
                response.text,
                response.reasoning,
                tuple(item.model_copy(deep=True) for item in response.tool_calls),
            )

    async def consume_inputs(self) -> tuple[MessageRecord, ...]:
        async with self._operation():
            if self._pending is not None:
                raise ValueError("resolve the pending model tool response before consuming inputs")
            # The algorithm may start a new decision without a model or tool
            # operation, for example after waiting on a plugin service.
            self._inputs_checked = False
            return tuple(message.model_copy(deep=True) for message in await self._deliver_inputs())

    async def _deliver_inputs(self) -> list[MessageRecord]:
        # A decision can explicitly read its inputs before requesting a model.
        # The implicit model boundary must not consume a second queue unit.
        if self._inputs_checked:
            return []
        if self.turn_id is None:
            self.turn_id = new_id("turn")
        delivered = await self._consume_inputs(
            self.sequence,
            session=self.session,
            run=self.run,
            turn_id=self.turn_id,
        )
        self.sequence = delivered.sequence
        self._inputs_checked = True
        if delivered.messages:
            self.final_text = ""
        return delivered.messages

    async def execute_model_tools(
        self, response_id: str, *, checkpoint: JsonValue
    ) -> DriverToolResult:
        async with self._operation():
            if self._pending is None or self._pending[0] != response_id:
                raise ValueError("model response handle is unavailable or already consumed")
            durable = DriverCheckpoint(batch_id=new_id("batch"), payload=checkpoint)
            _, turn_id, response = self._pending
            self._pending = None
            result = await self._request_tools(
                ModelToolBatchRequest(
                    session=self.session,
                    run=self.run,
                    response=response,
                    turn_id=turn_id,
                    sequence=self.sequence,
                    abort_signal=self.signal,
                    driver_checkpoint=durable,
                )
            )
            return await self._accept_tool_result(result)

    async def execute_tools(
        self, calls: tuple[ToolCallRequest, ...], *, checkpoint: JsonValue
    ) -> DriverToolResult:
        async with self._operation():
            if self._pending is not None:
                raise ValueError(
                    "resolve the pending model tool response before programmatic calls"
                )
            self._response = None
            result = await self._request_driver_tools(
                DriverToolBatchRequest(
                    session=self.session,
                    run=self.run,
                    calls=calls,
                    turn_id=new_id("turn"),
                    sequence=self.sequence,
                    abort_signal=self.signal,
                    checkpoint=DriverCheckpoint(batch_id=new_id("batch"), payload=checkpoint),
                )
            )
            return await self._accept_tool_result(result)

    async def _accept_tool_result(self, result: ToolBatchHandlingResult) -> DriverToolResult:
        self.session, self.run, self.sequence = result.session, result.run, result.sequence
        self.had_tool_error |= any(item.is_error for item in result.results)
        self._inputs_checked = False
        self.final_text = ""
        self.suspended = result.status != "completed"
        if self.suspended:
            return DriverToolResult(True)
        if self.is_aborted:
            raise asyncio.CancelledError
        self.run, resume = await run_blocking(
            self._runs.consume_driver_checkpoint, self.session_id, self.run_id
        )
        return DriverToolResult(False, tuple(item.model_copy(deep=True) for item in resume.results))

    def model_response(self, response_id: str) -> ModelExecutionResult:
        if self._response is None or self._response[0] != response_id:
            raise ValueError("model response handle is unavailable")
        return self._response[1]

    async def pause(self, reason: RunPauseReason) -> Never:
        self.stopped = await self._runs.pause(
            session=self.session,
            run=self.run,
            reason=reason,
            next_model_round=self.run.model_round_count + 1,
            final_text=self.final_text,
            had_tool_error=self.had_tool_error,
        )
        raise _DriverHalted

    async def fail(self, error: str, *, response: ModelExecutionResult | None = None) -> Never:
        self.stopped = await self._runs.fail(
            session=self.session,
            run=self.run,
            error=error,
            error_code=response.error_code if response is not None else None,
            final_text=response.text if response is not None else "",
            reasoning_content=response.reasoning if response is not None else "",
            model_response=response.model_response if response is not None else None,
            provider_continuation=response.continuation if response is not None else None,
            turn_id=self.turn_id,
            had_tool_error=self.had_tool_error,
        )
        raise _DriverHalted

    async def invoke(self, handler: RunDriverHandler, *, resuming: bool = False) -> RunResult:
        resume: DriverResume | None = None
        try:
            if resuming:
                checkpoint = self.run.driver_checkpoint
                if not self.run.driver.is_default or (
                    checkpoint is not None and checkpoint.state == "ready"
                ):
                    self.run, checkpoint_resume = await run_blocking(
                        self._runs.consume_driver_checkpoint, self.session_id, self.run_id
                    )
                    self.had_tool_error |= any(item.is_error for item in checkpoint_resume.results)
                    resume = checkpoint_resume
                elif self.turn_id is not None:
                    # The built-in algorithm can continue from native facts after
                    # interruption; arbitrary plugin code requires an unused checkpoint.
                    resume = DriverResume("turn", ())
            task = asyncio.ensure_future(handler(self, resume))
            cancellation = asyncio.create_task(self.signal.wait())
            try:
                done, _ = await asyncio.wait(
                    (task, cancellation), return_when=asyncio.FIRST_COMPLETED
                )
                if task not in done:
                    # Native ports own cancellation of their work and durable
                    # outcomes. Cancel arbitrary Driver code only after they settle.
                    await self._operation_idle.wait()
                    task.cancel()
                outcome = await task
            finally:
                self._active = False
                operation = self._operation_task
                unawaited = operation is not None and operation is not task
                task.cancel()
                cancellation.cancel()
                await asyncio.gather(task, cancellation, return_exceptions=True)
                if unawaited:
                    assert operation is not None
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
            if unawaited:
                raise ValueError("Driver must await every native operation before returning")
            if self.stopped is not None:
                return self.stopped
            if self.suspended:
                if not isinstance(outcome, DriverSuspended):
                    raise ValueError("a waiting Driver must return DriverSuspended")
                return RunResult(
                    session=self.session,
                    run=self.run,
                    had_tool_error=self.had_tool_error,
                )
            if not isinstance(outcome, DriverFinished):
                raise ValueError("DriverSuspended requires a native waiting operation")
            if self._pending is not None:
                raise ValueError("Driver cannot finish with unresolved model tool calls")
            committed = outcome.response_id is not None
            if outcome.response_id is not None:
                response = self.model_response(outcome.response_id)
                if response.tool_calls or outcome.text != response.text:
                    raise ValueError("final response must match the committed model output")
            return await self._runs.complete(
                session=self.session,
                run=self.run,
                final_text=outcome.text,
                had_tool_error=self.had_tool_error,
                assistant_already_committed=committed,
            )
        except _DriverHalted:
            assert self.stopped is not None
            return self.stopped
        except asyncio.CancelledError:
            if not self.is_aborted:
                raise
            return RunResult(
                session=self.session,
                run=self.run,
                had_tool_error=self.had_tool_error,
                final_text=self.final_text,
            )
        except Exception as exc:  # noqa: BLE001 - Driver execution is an extension boundary.
            if self.stopped is not None:
                return self.stopped
            return await self._runs.fail(
                session=self.session,
                run=self.run,
                error=f"Run driver failed: {type(exc).__name__}: {redact_text(str(exc))}",
                final_text="",
                had_tool_error=self.had_tool_error,
            )
        finally:
            self.close()
