from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ouro.core.cancellation import AbortSignal
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.model_execution import ModelExecutionResult
from ouro.core.run_budget import RunPauseReason
from ouro.core.state import RunRecord, SessionRecord


@dataclass(frozen=True)
class RunModelRequest:
    session: SessionRecord
    run: RunRecord
    messages: list[MessageRecord]
    model_round: int
    turn_id: str
    sequence: int
    emitted: list[EventRecord]
    abort_signal: AbortSignal
    prompt: str = ""
    tools: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ModelRequestBlocked:
    reason: RunPauseReason


@dataclass(frozen=True)
class ModelRequestFailed:
    error: str


@dataclass(frozen=True)
class RunModelResult:
    session: SessionRecord
    run: RunRecord
    sequence: int
    outcome: ModelExecutionResult | ModelRequestBlocked | ModelRequestFailed


RunModelRequester = Callable[[RunModelRequest], Awaitable[RunModelResult]]
