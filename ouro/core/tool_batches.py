from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from ouro.core.agents import AgentWaitRequest
from ouro.core.approvals import ApprovalRecord
from ouro.core.base import Metadata, MetadataField, OuroModel, VersionedOuroModel, new_id, utc_now
from ouro.core.harness_calls import HarnessStateChange
from ouro.core.interactions import ToolInputResolution
from ouro.core.session_controls import SessionControlTransitionRequest
from ouro.core.tool_facts import ToolCallFacts, ToolCallIdentity, ToolFactConflict
from ouro.core.tools import (
    ToolCallProducer,
    ToolCallRequest,
    ToolInputRequest,
    ToolInvocation,
    ToolInvocationBinding,
    ToolResult,
    ToolResultFeedback,
)

ToolExecutionMode = Literal["sequential", "parallel"]
ToolBatchPhase = Literal[
    "planned",
    "awaiting_interaction",
    "awaiting_child",
    "ready",
    "executing",
    "committing_results",
    "completed",
    "aborted",
    "recoverable",
]
ToolCallStatus = Literal[
    "planned",
    "awaiting_approval",
    "waiting_user",
    "waiting_child",
    "resolving_input",
    "ready",
    "rejected",
    "running",
    "completed",
    "failed",
    "aborted",
    "unknown_outcome",
]
ToolBatchOutcomeStatus = Literal["completed", "waiting_approval", "waiting_user", "waiting_child"]


class ToolBatchCall(OuroModel):
    """An explicitly loaded execution value, never embedded in a durable Run."""

    request: ToolCallRequest
    status: ToolCallStatus = "planned"
    invocation: ToolInvocation | None = None
    approval: ApprovalRecord | None = None
    input_request: ToolInputRequest | None = None
    input_resolution: ToolInputResolution | None = None
    input_revision: int = Field(default=0, ge=0)
    agent_wait: AgentWaitRequest | None = None
    result: ToolResult | None = None
    feedback: ToolResultFeedback | None = None
    result_message_id: str | None = None
    attempt: int = Field(default=0, ge=0)
    metadata: Metadata = MetadataField


class ToolCallBatchRecord(OuroModel):
    """The selected active batch, with values loaded for its execution or publication."""

    batch_id: str = Field(default_factory=lambda: new_id("batch"))
    turn_id: str
    assistant_message_id: str | None = None
    producer: ToolCallProducer = Field(default_factory=ToolCallProducer)
    phase: ToolBatchPhase = "planned"
    execution_mode: ToolExecutionMode = "sequential"
    max_concurrency: int = Field(default=4, ge=1)
    calls: list[ToolBatchCall] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: Metadata = MetadataField

    @model_validator(mode="after")
    def validate_origin(self) -> Self:
        if (self.producer.kind == "model") != bool(self.assistant_message_id):
            raise ValueError("only model tool batches require an assistant message")
        return self

    @property
    def pending_approvals(self) -> list[ApprovalRecord]:
        return [
            call.approval
            for call in self.calls
            if call.status == "awaiting_approval" and call.approval is not None
        ]

    @property
    def is_interaction_resolved(self) -> bool:
        return all(
            call.status not in {"awaiting_approval", "waiting_user", "waiting_child"}
            for call in self.calls
        )

    def head(self) -> ToolBatchHead:
        return ToolBatchHead(
            **self.model_dump(exclude={"calls"}),
            call_ids=tuple(call.request.id for call in self.calls),
        )


class ToolBatchHead(OuroModel):
    """The Run's bounded current-batch references, with no request or result bodies."""

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    turn_id: str
    assistant_message_id: str | None = None
    producer: ToolCallProducer = Field(default_factory=ToolCallProducer)
    phase: ToolBatchPhase = "planned"
    execution_mode: ToolExecutionMode = "sequential"
    max_concurrency: int = Field(default=4, ge=1)
    call_ids: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    metadata: Metadata = MetadataField

    @model_validator(mode="after")
    def validate_membership(self) -> Self:
        if (self.producer.kind == "model") != bool(self.assistant_message_id):
            raise ValueError("only model tool batches require an assistant message")
        if len(set(self.call_ids)) != len(self.call_ids):
            raise ValueError("tool batch contains duplicate call identities")
        return self

    def identity(self, run_id: str, tool_call_id: str) -> ToolCallIdentity:
        if tool_call_id not in self.call_ids:
            raise ValueError("Call is not a member of this batch")
        return ToolCallIdentity(run_id=run_id, batch_id=self.batch_id, tool_call_id=tool_call_id)


class ToolResultProposals(OuroModel):
    """Unaccepted business effects; cleared when their publication is committed."""

    model_config = ConfigDict(extra="forbid")

    harness_change: HarnessStateChange | None = None
    session_control_transitions: list[SessionControlTransitionRequest] = Field(default_factory=list)

    @classmethod
    def from_result(cls, result: ToolResult) -> ToolResultProposals | None:
        if not (result.harness_change or result.session_control_transitions):
            return None
        return cls.model_validate(result.model_dump(include=set(cls.model_fields)))


class ToolCallState(VersionedOuroModel):
    """One Call's waiting, execution and publication state, separately from its facts."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    identity: ToolCallIdentity
    status: ToolCallStatus = "planned"
    attempt: int = Field(default=0, ge=0)
    # Preflight binding has no copied original arguments. After starting, the
    # immutable start owns the actual parameters and execution provenance.
    prepared_binding: ToolInvocationBinding | None = None
    arguments_override: dict[str, object] | None = None
    approval: ApprovalRecord | None = None
    input_request: ToolInputRequest | None = None
    input_resolution: ToolInputResolution | None = None
    input_revision: int = Field(default=0, ge=0)
    interaction_activation_id: str | None = None
    agent_wait: AgentWaitRequest | None = None
    outcome_attempt: int | None = Field(default=None, ge=0)
    proposals: ToolResultProposals | None = None
    feedback: ToolResultFeedback | None = None
    result_message_id: str | None = None
    metadata: Metadata = MetadataField

    @model_validator(mode="after")
    def validate_ownership(self) -> Self:
        if self.outcome_attempt is not None and self.outcome_attempt != self.attempt:
            raise ValueError("Call state Outcome must belong to its current attempt")
        if self.approval is not None and self.approval.modified_arguments is not None:
            raise ValueError("approval parameters belong to the prepared Call or its start")
        if self.arguments_override is not None and self.prepared_binding is None:
            raise ValueError("prepared arguments require their execution binding")
        if self.prepared_binding is not None and (
            self.prepared_binding.session_id != self.session_id
            or self.prepared_binding.run_id != self.identity.run_id
            or self.prepared_binding.tool_call_id != self.identity.tool_call_id
        ):
            raise ValueError("prepared execution belongs to another Call")
        if self.result_message_id is not None and self.proposals is not None:
            raise ValueError("published Call cannot retain pending business proposals")
        if self.feedback is not None and (
            self.outcome_attempt is None or self.result_message_id is not None
        ):
            raise ValueError("feedback requires an unpublished observed Outcome")
        return self


def validate_call_states(
    states: tuple[ToolCallState, ...],
    facts: ToolCallFacts,
    *,
    session_id: str,
    active_runs: Mapping[str, int] | None = None,
    history_epoch: int | None = None,
) -> None:
    """Check only the immutable facts referenced by the proposed Call states."""
    if len({state.identity for state in states}) != len(states):
        raise ToolFactConflict("duplicate Call state identity")
    calls = {call.identity: call for call in facts.calls}
    starts = {(start.identity, start.attempt): start for start in facts.starts}
    outcomes = {(outcome.identity, outcome.attempt): outcome for outcome in facts.outcomes}
    for state in states:
        call = calls.get(state.identity)
        if call is None or call.session_id != session_id or state.session_id != session_id:
            raise ToolFactConflict("Call state has no accepted Call in this Session")
        if state.attempt > 0 and (state.identity, state.attempt) not in starts:
            raise ToolFactConflict("Call state has no durable start for its attempt")
        if (
            state.outcome_attempt is not None
            and (state.identity, state.outcome_attempt) not in outcomes
        ):
            raise ToolFactConflict("Call state has no matching certain Outcome")
        if state.prepared_binding is not None and (
            state.prepared_binding.tool_name != call.request.name
            or state.prepared_binding.turn_id != call.turn_id
            or state.prepared_binding.producer != call.producer
        ):
            raise ToolFactConflict("prepared binding does not belong to the accepted Call")
        if state.arguments_override == call.request.arguments:
            raise ToolFactConflict("unchanged prepared arguments must reference the Call")
        if (
            active_runs is not None
            and (
                state.status
                in {
                    "planned",
                    "awaiting_approval",
                    "waiting_user",
                    "waiting_child",
                    "resolving_input",
                    "ready",
                    "running",
                }
                or state.proposals is not None
            )
            and (
                call.origin_session_id != session_id
                or call.history_epoch != history_epoch
                or active_runs.get(state.identity.run_id) != history_epoch
            )
        ):
            raise ToolFactConflict("pending Call state requires its current active Run")
