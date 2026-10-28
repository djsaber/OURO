from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal, Protocol

from pydantic import ConfigDict, Field, model_validator

from ouro.core.base import Metadata, MetadataField, OuroModel, VersionedOuroModel, new_id, utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.context import (
    ContextMetrics,
    TokenEstimator,
    estimate_message_tokens,
    estimate_text_tokens,
)
from ouro.core.errors import CompactionError, CompactionStateError, PersistenceError
from ouro.core.events import EventRecord
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.core.message_frames import context_frames as compaction_frames
from ouro.core.message_frames import conversation_turns as conversation_turns
from ouro.core.messages import MessageFact, MessageRecord
from ouro.core.provider import ProviderFinishReason
from ouro.core.state import RunRecord, SessionRecord

CompactionTrigger = Literal["auto", "manual"]


class CompactionPolicy(OuroModel):
    model_config = ConfigDict(extra="forbid")

    auto_enabled: bool = True
    trigger_ratio: float = Field(default=0.8, gt=0.0, le=1.0)
    target_ratio: float = Field(default=0.2, gt=0.0, lt=1.0)

    @model_validator(mode="after")
    def _validate_ratios(self) -> CompactionPolicy:
        if self.target_ratio >= self.trigger_ratio:
            raise ValueError("compaction target_ratio must be lower than trigger_ratio")
        return self


class CompactionRequest(OuroModel):
    model_config = ConfigDict(extra="forbid")

    harness: HarnessCallSnapshot | None = None
    session_id: str
    run_id: str | None = None
    strategy_id: str
    trigger: CompactionTrigger
    agent_system_prompt: str = ""
    previous_summary: str | None = None
    messages: list[MessageRecord] = Field(default_factory=list)


class CompactionModelOutput(OuroModel):
    """Provider-neutral evidence captured for one compaction model request."""

    request_id: str = Field(min_length=1)
    route: str = Field(min_length=1)
    chunk_index: int = Field(ge=1)
    attempt_index: int = Field(ge=1)
    status: Literal["completed", "failed"]
    requested_output_tokens: int | None = Field(default=None, ge=1, strict=True)
    estimated_input_tokens: int = Field(ge=0)
    input_message_count: int = Field(ge=0)
    input_frame_count: int = Field(ge=1)
    finish_reason: ProviderFinishReason
    provider_finish_reason: str | None = None
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
        if self.status == "failed" and (not self.error_type or self.error is None):
            raise ValueError("failed compaction model output requires error details")
        if self.status == "completed" and (self.error_type is not None or self.error is not None):
            raise ValueError("completed compaction model output cannot contain an error")
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
    schema_version: Literal[3] = 3
    compaction_id: str = Field(default_factory=lambda: new_id("cmp"))
    session_id: str
    previous_compaction_id: str | None = None
    strategy_id: str
    strategy_version: str = "1"
    trigger: CompactionTrigger
    summary: str
    covered_message_count: int = Field(ge=1)
    covered_end_message_id: str = Field(min_length=1)
    retained_skill_message_ids: tuple[str, ...] = ()
    history_boundary_message_id: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    duration_seconds: float | None = Field(default=None, ge=0.0)
    model_provider: str | None = None
    model_endpoint_id: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    estimated_tokens_before: int = 0
    estimated_summary_tokens: int = 0
    estimated_tokens_after: int = 0
    context_metrics_after: ContextMetrics | None = None
    usage: dict[str, int | float] = Field(default_factory=dict)
    metadata: Metadata = MetadataField

    @property
    def referenced_message_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    self.covered_end_message_id,
                    self.history_boundary_message_id,
                    *self.retained_skill_message_ids,
                )
            )
        )


def validate_compaction_sources(
    record: CompactionRecord,
    session_id: str,
    message_count: int,
    sources: Mapping[str, MessageFact | MessageRecord],
) -> None:
    """Check the finite references at activation or snapshot publication."""
    endpoint = sources.get(record.covered_end_message_id)
    placement = sources.get(record.history_boundary_message_id)
    if (
        record.session_id != session_id
        or endpoint is None
        or endpoint.sequence != record.covered_message_count
        or endpoint.session_id != session_id
        or placement is None
        or placement.session_id != session_id
        or not endpoint.sequence <= placement.sequence <= message_count
    ):
        raise CompactionStateError("compaction boundary does not match canonical history")
    if any(
        (source := sources.get(identity)) is None
        or source.session_id != session_id
        or not 1 <= source.sequence <= endpoint.sequence
        for identity in record.retained_skill_message_ids
    ):
        raise CompactionStateError("compaction skill reference is outside its coverage")


def validate_compaction_snapshot(
    session: SessionRecord,
    messages: Sequence[MessageRecord],
    records: Sequence[CompactionRecord],
) -> None:
    """Validate imported dependencies once before publishing a new Session."""
    if not records and session.context_selection.compaction_id is None:
        return
    sources = {item.message_id: item for item in messages}
    accepted: set[str] = set()
    for record in records:
        if record.compaction_id in accepted:
            raise PersistenceError("Session snapshot contains duplicate compaction identities")
        if (
            record.previous_compaction_id is not None
            and record.previous_compaction_id not in accepted
        ):
            raise PersistenceError("Session snapshot has a missing compaction predecessor")
        try:
            validate_compaction_sources(record, session.session_id, len(messages), sources)
        except CompactionStateError as exc:
            raise PersistenceError(str(exc)) from exc
        accepted.add(record.compaction_id)
    if (
        session.context_selection.compaction_id is not None
        and session.context_selection.compaction_id not in accepted
    ):
        raise PersistenceError("Session snapshot has a missing active compaction")


class CompactionPlan(OuroModel):
    messages_to_compact: list[MessageRecord] = Field(default_factory=list)
    retained_messages: list[MessageRecord] = Field(default_factory=list)


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
    *,
    tail_token_budget: int,
    force: bool = False,
    token_estimator: TokenEstimator | None = None,
) -> CompactionPlan:
    """Select a prefix of unsummarized input, preserving the latest context frame."""

    estimator = token_estimator or estimate_text_tokens
    frames = compaction_frames(messages)
    if len(frames) <= 1:
        return CompactionPlan(retained_messages=list(messages))

    retained_start = len(frames) - 1
    retained_tokens = _messages_tokens(frames[-1], estimator)
    while retained_start > 0:
        frame = frames[retained_start - 1]
        frame_tokens = _messages_tokens(frame, estimator)
        if retained_tokens + frame_tokens > tail_token_budget:
            break
        retained_start -= 1
        retained_tokens += frame_tokens

    if force and retained_start == 0 and len(frames) > 1:
        retained_start = 1

    messages_to_compact = [message for frame in frames[:retained_start] for message in frame]
    retained_messages = [message for frame in frames[retained_start:] for message in frame]
    return CompactionPlan(
        messages_to_compact=messages_to_compact,
        retained_messages=retained_messages,
    )


def project_compacted_messages(
    uncovered_messages: Sequence[MessageRecord],
    active: CompactionRecord | None,
) -> list[MessageRecord]:
    """Prepend the selected summary to its already selected canonical suffix."""
    if active is None:
        return list(uncovered_messages)
    covered_count = active.covered_message_count
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
    return [summary, *uncovered_messages]


def _messages_tokens(messages: Sequence[MessageRecord], estimator: TokenEstimator) -> int:
    return sum(estimate_message_tokens(message, estimator) for message in messages)


class CompactionReference(OuroModel):
    """Withdrawal dependencies and ordered membership, without the summary body."""

    compaction_id: str
    previous_compaction_id: str | None = None
    covered_end_message_id: str
    history_boundary_message_id: str

    @classmethod
    def from_record(cls, record: CompactionRecord) -> CompactionReference:
        return cls(
            compaction_id=record.compaction_id,
            previous_compaction_id=record.previous_compaction_id,
            covered_end_message_id=record.covered_end_message_id,
            history_boundary_message_id=record.history_boundary_message_id,
        )


def retain_compaction_references(
    compactions: Sequence[CompactionReference],
    removed_message_ids: set[str],
    *,
    last_retained_message_id: str | None,
) -> list[CompactionReference]:
    """Withdraw affected coverage; a removed display anchor does not invalidate content."""
    retained: list[CompactionReference] = []
    removed: set[str] = set()
    for compaction in compactions:
        if (
            compaction.covered_end_message_id in removed_message_ids
            or compaction.previous_compaction_id in removed
        ):
            removed.add(compaction.compaction_id)
        else:
            if compaction.history_boundary_message_id in removed_message_ids:
                assert last_retained_message_id is not None
                compaction = compaction.model_copy(
                    update={
                        "history_boundary_message_id": last_retained_message_id,
                    }
                )
            retained.append(compaction)
    return retained
