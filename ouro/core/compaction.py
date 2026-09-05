from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field, model_validator

from ouro.core.base import Metadata, MetadataField, OuroModel, VersionedOuroModel, new_id, utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.context import (
    ContextMetrics,
    TokenEstimator,
    estimate_message_tokens,
    estimate_text_tokens,
)
from ouro.core.errors import CompactionError, CompactionStateError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.provider import ProviderFinishReason
from ouro.core.state import RunRecord, SessionRecord

CompactionTrigger = Literal["auto", "manual"]


class CompactionPolicy(OuroModel):
    auto_enabled: bool = True
    trigger_ratio: float = Field(default=0.8, gt=0.0, le=1.0)
    target_ratio: float = Field(default=0.2, gt=0.0, lt=1.0)

    @model_validator(mode="after")
    def _validate_ratios(self) -> CompactionPolicy:
        if self.target_ratio >= self.trigger_ratio:
            raise ValueError("compaction target_ratio must be lower than trigger_ratio")
        return self


class CompactionRequest(OuroModel):
    session_id: str
    run_id: str | None = None
    strategy_id: str
    trigger: CompactionTrigger
    previous_summary: str | None = None
    messages: list[MessageRecord] = Field(default_factory=list)
    input_token_budget: int = Field(ge=1)
    summary_output_tokens: int = Field(ge=1)


class CompactionModelOutput(OuroModel):
    """Provider-neutral evidence captured for one compaction model request."""

    request_id: str = Field(min_length=1)
    route: str = Field(min_length=1)
    chunk_index: int = Field(ge=1)
    attempt_index: int = Field(ge=1)
    status: Literal["completed", "failed"]
    requested_output_tokens: int = Field(ge=1)
    estimated_input_tokens: int = Field(ge=0)
    input_message_count: int = Field(ge=0)
    input_frame_count: int = Field(ge=1)
    finish_reason: ProviderFinishReason
    provider_finish_reason: str | None = None
    summary_opened: bool
    summary_closed: bool
    raw_text: str = ""
    raw_reasoning: str = ""
    model_provider: str = Field(min_length=1)
    model_endpoint_id: str | None = None
    model_id: str = Field(min_length=1)
    usage: dict[str, int | float] = Field(default_factory=dict)
    error_type: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _validate_status(self) -> CompactionModelOutput:
        if self.summary_closed and not self.summary_opened:
            raise ValueError("closed compaction summary requires an opening tag")
        if self.status == "failed" and (not self.error_type or self.error is None):
            raise ValueError("failed compaction model output requires error details")
        if self.status == "completed" and (self.error_type is not None or self.error is not None):
            raise ValueError("completed compaction model output cannot contain an error")
        if self.status == "completed" and not self.summary_closed:
            raise ValueError("completed compaction model output requires a closed summary")
        return self


class CompactionArtifact(OuroModel):
    summary: str
    usage: dict[str, int | float] = Field(default_factory=dict)
    model_outputs: list[CompactionModelOutput] = Field(default_factory=list)
    metadata: Metadata = MetadataField


class CompactionStrategyError(CompactionError):
    """A strategy failure carrying model outputs that must remain diagnosable."""

    def __init__(
        self,
        message: str,
        *,
        model_outputs: Sequence[CompactionModelOutput] = (),
        terminal: bool = False,
    ) -> None:
        super().__init__(message)
        self.model_outputs = tuple(model_outputs)
        self.terminal = terminal


class CompactionAbortedError(CompactionStrategyError):
    """Compaction stopped at the Host cancellation boundary."""

    def __init__(
        self,
        reason: str,
        *,
        model_outputs: Sequence[CompactionModelOutput] = (),
    ) -> None:
        super().__init__(reason, model_outputs=model_outputs, terminal=True)


class CompactionRecord(VersionedOuroModel):
    compaction_id: str = Field(default_factory=lambda: new_id("cmp"))
    session_id: str
    previous_compaction_id: str | None = None
    strategy_id: str
    strategy_version: str = "1"
    trigger: CompactionTrigger
    summary: str
    covered_message_ids: list[str] = Field(default_factory=list)
    source_digest: str
    history_boundary_message_id: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    duration_seconds: float | None = Field(default=None, ge=0.0)
    model_provider: str | None = None
    model_endpoint_id: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    prompt_hash: str | None = None
    estimated_tokens_before: int = 0
    estimated_summary_tokens: int = 0
    estimated_tokens_after: int = 0
    context_metrics_after: ContextMetrics | None = None
    usage: dict[str, int | float] = Field(default_factory=dict)
    metadata: Metadata = MetadataField


class CompactionPlan(OuroModel):
    messages_to_compact: list[MessageRecord] = Field(default_factory=list)
    retained_messages: list[MessageRecord] = Field(default_factory=list)
    covered_message_ids: list[str] = Field(default_factory=list)


class CompactionCommandResult(OuroModel):
    session: SessionRecord
    run: RunRecord
    compaction: CompactionRecord | None = None
    events: list[EventRecord] = Field(default_factory=list)
    skipped: bool = False


class CompactionStrategy(Protocol):
    strategy_id: str
    version: str

    async def compact(
        self,
        request: CompactionRequest,
        *,
        signal: AbortSignal | None = None,
    ) -> CompactionArtifact: ...


class CompactionStrategyRegistry:
    def __init__(self) -> None:
        self._strategies: dict[str, CompactionStrategy] = {}
        self._owners: dict[str, str] = {}
        self._frozen = False

    def freeze(self) -> None:
        self._frozen = True

    def _require_mutable(self) -> None:
        if self._frozen:
            raise RuntimeError("compaction registry structure is frozen")

    def register(self, strategy: CompactionStrategy) -> None:
        self.replace((), (strategy,))

    def replace(
        self,
        previous_ids: Sequence[str],
        strategies: Sequence[CompactionStrategy],
    ) -> None:
        self._require_mutable()
        updated = dict(self._strategies)
        owners = dict(self._owners)
        for strategy_id in previous_ids:
            updated.pop(strategy_id, None)
            owners.pop(strategy_id, None)
        for strategy in strategies:
            if strategy.strategy_id in updated:
                raise ValueError(f"compaction strategy already registered: {strategy.strategy_id}")
            updated[strategy.strategy_id] = strategy
        self._strategies = updated
        self._owners = owners

    def replace_owner(
        self,
        owner: str,
        strategies: Sequence[CompactionStrategy],
    ) -> None:
        self._require_mutable()
        retained = {
            identity: strategy
            for identity, strategy in self._strategies.items()
            if self._owners.get(identity) != owner
        }
        owners = {
            identity: item_owner
            for identity, item_owner in self._owners.items()
            if item_owner != owner
        }
        identities = [strategy.strategy_id for strategy in strategies]
        if len(identities) != len(set(identities)):
            raise ValueError("replacement contains duplicate compaction strategy ids")
        for strategy in strategies:
            if strategy.strategy_id in retained:
                raise ValueError(f"compaction strategy already registered: {strategy.strategy_id}")
            retained[strategy.strategy_id] = strategy
            owners[strategy.strategy_id] = owner
        self._strategies = retained
        self._owners = owners

    def unregister_owner(self, owner: str) -> None:
        self.replace_owner(owner, ())

    def snapshot(
        self,
    ) -> tuple[dict[str, CompactionStrategy], dict[str, str]]:
        return dict(self._strategies), dict(self._owners)

    def restore(
        self,
        snapshot: tuple[dict[str, CompactionStrategy], dict[str, str]],
    ) -> None:
        self._require_mutable()
        self._strategies, self._owners = dict(snapshot[0]), dict(snapshot[1])

    def get(self, strategy_id: str) -> CompactionStrategy:
        try:
            return self._strategies[strategy_id]
        except KeyError as exc:
            raise KeyError(f"unknown compaction strategy: {strategy_id}") from exc

    def list(self) -> list[CompactionStrategy]:
        return [self._strategies[key] for key in sorted(self._strategies)]


def plan_compaction(
    messages: Sequence[MessageRecord],
    active: CompactionRecord | None,
    *,
    tail_token_budget: int,
    force: bool = False,
    token_estimator: TokenEstimator | None = None,
    protected_run_id: str | None = None,
) -> CompactionPlan:
    """Select a canonical prefix while preserving the latest context frame verbatim."""

    estimator = token_estimator or estimate_text_tokens
    covered_count = validate_compaction_lineage(messages, active)
    prior_ids = list(active.covered_message_ids) if active else []
    uncovered = list(messages[covered_count:])
    frames = compaction_frames(uncovered)
    if len(frames) <= 1:
        return CompactionPlan(retained_messages=uncovered, covered_message_ids=prior_ids)

    protected_index = next(
        (
            index
            for index, frame in enumerate(frames)
            if protected_run_id is not None
            and any(message.run_id == protected_run_id for message in frame)
        ),
        len(frames) - 1,
    )
    retained_start = protected_index
    retained_tokens = _messages_tokens(
        [message for frame in frames[retained_start:] for message in frame],
        estimator,
    )
    while retained_start > 0:
        frame = frames[retained_start - 1]
        frame_tokens = _messages_tokens(frame, estimator)
        if retained_tokens + frame_tokens > tail_token_budget:
            break
        retained_start -= 1
        retained_tokens += frame_tokens

    if force and protected_run_id is None and retained_start == 0 and len(frames) > 1:
        retained_start = 1

    messages_to_compact = [message for frame in frames[:retained_start] for message in frame]
    retained_messages = [message for frame in frames[retained_start:] for message in frame]
    return CompactionPlan(
        messages_to_compact=messages_to_compact,
        retained_messages=retained_messages,
        covered_message_ids=[*prior_ids, *(message.message_id for message in messages_to_compact)],
    )


def conversation_turns(messages: Sequence[MessageRecord]) -> list[list[MessageRecord]]:
    """Group messages by user requests when no model-turn identity is available."""

    turns: list[list[MessageRecord]] = []
    current: list[MessageRecord] = []
    for message in messages:
        if message.role == "user" and current:
            turns.append(current)
            current = []
        current.append(message)
    if current:
        turns.append(current)
    return turns


def compaction_frames(messages: Sequence[MessageRecord]) -> list[list[MessageRecord]]:
    """Group messages at durable model-turn boundaries.

    Messages in a model/tool exchange share a ``turn_id``. A preceding user message has no
    ``turn_id`` and joins the following identified frame. Messages outside a model/tool exchange
    are grouped by user request. If an incomplete tool exchange is encountered, the remaining
    suffix becomes one protected frame so compaction cannot separate it from recovery context.
    """

    frames: list[list[MessageRecord]] = []
    current: list[MessageRecord] = []
    current_turn_id: str | None = None
    pending_messages: list[MessageRecord] = []

    def flush_current() -> None:
        nonlocal current, current_turn_id
        if current:
            frames.append(current)
        current = []
        current_turn_id = None

    for message in messages:
        if message.turn_id is not None:
            if current and current_turn_id != message.turn_id:
                flush_current()
            if not current:
                pending_frames = conversation_turns(pending_messages)
                frames.extend(pending_frames[:-1])
                current = list(pending_frames[-1]) if pending_frames else []
                pending_messages = []
                current_turn_id = message.turn_id
            current.append(message)
            continue

        if current_turn_id is not None:
            flush_current()
        pending_messages.append(message)

    flush_current()
    frames.extend(conversation_turns(pending_messages))
    for index, frame in enumerate(frames):
        if _is_complete_frame(frame):
            continue
        return [*frames[:index], [message for tail in frames[index:] for message in tail]]
    return frames


def project_compacted_messages(
    messages: Sequence[MessageRecord],
    active: CompactionRecord | None,
) -> list[MessageRecord]:
    covered_count = validate_compaction_lineage(messages, active)
    if active is None:
        return list(messages)
    summary = MessageRecord.text(
        session_id=active.session_id,
        role="system",
        text=active.summary,
        sequence=-1,
        scaffold=True,
        semantic_tag="conversation-summary",
    ).model_copy(
        update={
            "metadata": {
                "kind": "compaction_summary",
                "compaction_id": active.compaction_id,
                "covered_message_count": covered_count,
                "strategy_id": active.strategy_id,
            }
        }
    )
    return [summary, *messages[covered_count:]]


def validate_compaction_lineage(
    messages: Sequence[MessageRecord],
    active: CompactionRecord | None,
) -> int:
    if active is None:
        return 0
    canonical_prefix = [
        message.message_id for message in messages[: len(active.covered_message_ids)]
    ]
    if canonical_prefix != active.covered_message_ids:
        raise CompactionStateError(
            f"compaction {active.compaction_id} does not match canonical message history"
        )
    digest = compaction_source_digest(messages[: len(active.covered_message_ids)])
    if digest != active.source_digest:
        raise CompactionStateError(
            f"compaction {active.compaction_id} source digest does not match canonical history"
        )
    return len(active.covered_message_ids)


def compaction_source_digest(messages: Sequence[MessageRecord]) -> str:
    digest = hashlib.sha256()
    for message in messages:
        digest.update(message.model_dump_json(exclude_none=True).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _messages_tokens(messages: Sequence[MessageRecord], estimator: TokenEstimator) -> int:
    return sum(estimate_message_tokens(message, estimator) for message in messages)


def _tool_call_ids(message: MessageRecord) -> set[str]:
    if message.role != "assistant":
        return set()
    return {call.id for call in message.tool_calls if call.id}


def _tool_result_id(message: MessageRecord) -> str | None:
    return message.tool_result.tool_call_id if message.tool_result is not None else None


def _is_complete_frame(frame: Sequence[MessageRecord]) -> bool:
    expected = {call_id for message in frame for call_id in _tool_call_ids(message)}
    if not expected:
        return True
    seen = {result_id for message in frame if (result_id := _tool_result_id(message)) is not None}
    return expected.issubset(seen)
