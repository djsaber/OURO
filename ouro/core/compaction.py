from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field, model_validator

from ouro.core.base import SCHEMA_VERSION, Metadata, MetadataField, OuroModel, new_id, utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.context import TokenEstimator, estimate_message_tokens, estimate_text_tokens
from ouro.core.errors import CompactionStateError
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.state import RunRecord, SessionRecord

CompactionTrigger = Literal["auto", "manual"]


class CompactionPolicy(OuroModel):
    auto_enabled: bool = True
    trigger_ratio: float = Field(default=0.8, gt=0.0, le=1.0)
    target_ratio: float = Field(default=0.2, gt=0.0, lt=1.0)
    summary_output_tokens: int = Field(default=8192, ge=256)

    @model_validator(mode="after")
    def _validate_ratios(self) -> CompactionPolicy:
        if self.target_ratio >= self.trigger_ratio:
            raise ValueError("compaction target_ratio must be lower than trigger_ratio")
        return self


class CompactionRequest(OuroModel):
    session_id: str
    strategy_id: str
    trigger: CompactionTrigger
    previous_summary: str | None = None
    messages: list[MessageRecord] = Field(default_factory=list)
    input_token_budget: int = Field(ge=1)
    summary_output_tokens: int = Field(ge=1)


class CompactionArtifact(OuroModel):
    summary: str
    usage: dict[str, int | float] = Field(default_factory=dict)
    metadata: Metadata = MetadataField


class CompactionRecord(OuroModel):
    schema_version: int = SCHEMA_VERSION
    compaction_id: str = Field(default_factory=lambda: new_id("cmp"))
    session_id: str
    previous_compaction_id: str | None = None
    strategy_id: str
    strategy_version: str = "1"
    trigger: CompactionTrigger
    summary: str
    covered_message_ids: list[str] = Field(default_factory=list)
    source_digest: str
    created_at: datetime = Field(default_factory=utc_now)
    model_provider: str | None = None
    model_endpoint_id: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    prompt_hash: str | None = None
    estimated_tokens_before: int = 0
    estimated_summary_tokens: int = 0
    estimated_tokens_after: int = 0
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

    def register(self, strategy: CompactionStrategy) -> None:
        if strategy.strategy_id in self._strategies:
            raise ValueError(f"compaction strategy already registered: {strategy.strategy_id}")
        self._strategies[strategy.strategy_id] = strategy

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
) -> CompactionPlan:
    """Select a canonical prefix while preserving the latest user turn verbatim."""

    estimator = token_estimator or estimate_text_tokens
    covered_count = validate_compaction_lineage(messages, active)
    prior_ids = list(active.covered_message_ids) if active else []
    uncovered = list(messages[covered_count:])
    turns = conversation_turns(uncovered)
    if len(turns) <= 1:
        return CompactionPlan(retained_messages=uncovered, covered_message_ids=prior_ids)

    retained_turns: list[list[MessageRecord]] = [turns[-1]]
    retained_tokens = _messages_tokens(turns[-1], estimator)
    candidate_turns = list(turns[:-1])
    while candidate_turns:
        turn = candidate_turns[-1]
        turn_tokens = _messages_tokens(turn, estimator)
        if retained_tokens + turn_tokens > tail_token_budget:
            break
        retained_turns.insert(0, candidate_turns.pop())
        retained_tokens += turn_tokens

    if force and not candidate_turns and len(retained_turns) > 1:
        candidate_turns.append(retained_turns.pop(0))

    messages_to_compact = [message for turn in candidate_turns for message in turn]
    retained_messages = [message for turn in retained_turns for message in turn]
    return CompactionPlan(
        messages_to_compact=messages_to_compact,
        retained_messages=retained_messages,
        covered_message_ids=[*prior_ids, *(message.message_id for message in messages_to_compact)],
    )


def conversation_turns(messages: Sequence[MessageRecord]) -> list[list[MessageRecord]]:
    """Group legacy and current messages by user-turn boundaries.

    Tool request/result exchanges remain in the same group because only a later user message closes
    the turn. This also works for older records whose declared ``turn_id`` is absent.
    """

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


def project_compacted_messages(
    messages: Sequence[MessageRecord],
    active: CompactionRecord | None,
) -> list[MessageRecord]:
    covered_count = validate_compaction_lineage(messages, active)
    if active is None:
        return list(messages)
    summary = MessageRecord.text(
        session_id=active.session_id,
        role="system_synthetic",
        text=active.summary,
        sequence=-1,
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
