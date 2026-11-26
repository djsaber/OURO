from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ouro.core.cancellation import AbortSignal
from ouro.core.model_execution import ModelExecutionResult
from ouro.core.run_drivers import DriverCheckpoint
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_batches import ToolBatchOutcomeStatus
from ouro.core.tools import ToolCallRequest


@dataclass(frozen=True)
class ModelToolBatchRequest:
    session: SessionRecord
    run: RunRecord
    response: ModelExecutionResult
    turn_id: str
    abort_signal: AbortSignal
    driver_checkpoint: DriverCheckpoint | None = None


@dataclass(frozen=True)
class ToolBatchHandlingResult:
    """A batch outcome and the Session/Run state committed by its owner."""

    session: SessionRecord
    run: RunRecord
    status: ToolBatchOutcomeStatus
    had_tool_error: bool = False


ModelToolBatchRequester = Callable[[ModelToolBatchRequest], Awaitable[ToolBatchHandlingResult]]


@dataclass(frozen=True)
class DriverToolBatchRequest:
    session: SessionRecord
    run: RunRecord
    calls: tuple[ToolCallRequest, ...]
    turn_id: str
    abort_signal: AbortSignal
    checkpoint: DriverCheckpoint


DriverToolBatchRequester = Callable[[DriverToolBatchRequest], Awaitable[ToolBatchHandlingResult]]
