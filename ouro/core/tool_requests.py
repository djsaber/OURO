from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.cancellation import AbortSignal
from ouro.core.messages import MessageRecord
from ouro.core.model_execution import ModelExecutionResult
from ouro.core.run_drivers import DriverCheckpoint
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_batches import ToolBatchOutcomeStatus, ToolCallBatchRecord
from ouro.core.tools import ToolCallRequest, ToolInputRequest, ToolResult


@dataclass(frozen=True)
class ModelToolBatchRequest:
    session: SessionRecord
    run: RunRecord
    response: ModelExecutionResult
    turn_id: str
    sequence: int
    abort_signal: AbortSignal
    driver_checkpoint: DriverCheckpoint | None = None


class ToolBatchHandlingResult(OuroModel):
    """A batch outcome and the Session/Run state committed by its owner."""

    session: SessionRecord
    run: RunRecord
    status: ToolBatchOutcomeStatus
    batch: ToolCallBatchRecord
    messages: list[MessageRecord] = Field(default_factory=list)
    results: list[ToolResult] = Field(default_factory=list)
    input_request: ToolInputRequest | None = None
    sequence: int = 0


ModelToolBatchRequester = Callable[[ModelToolBatchRequest], Awaitable[ToolBatchHandlingResult]]


@dataclass(frozen=True)
class DriverToolBatchRequest:
    session: SessionRecord
    run: RunRecord
    calls: tuple[ToolCallRequest, ...]
    turn_id: str
    sequence: int
    abort_signal: AbortSignal
    checkpoint: DriverCheckpoint


DriverToolBatchRequester = Callable[[DriverToolBatchRequest], Awaitable[ToolBatchHandlingResult]]
