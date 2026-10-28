from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any, Self

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic.dataclasses import dataclass

from ouro.core.base import OuroModel, VersionedOuroModel, utc_now
from ouro.core.content import MediaContent, media_data_uri, media_reference
from ouro.core.errors import PersistenceError
from ouro.core.tools import (
    ToolCallProducer,
    ToolCallRequest,
    ToolInvocation,
    ToolInvocationBinding,
    ToolResult,
    ToolResultValue,
)


@dataclass(frozen=True, slots=True, config=ConfigDict(extra="forbid"))
class ToolCallIdentity:
    """A Provider call ID is only unique inside its owning Run and native batch."""

    run_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        # Three immutable strings need no duplicate identity in copied fact graphs.
        return self


class ToolCallRecord(VersionedOuroModel):
    """The original, immutable call accepted from a model or Driver."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    identity: ToolCallIdentity
    origin_session_id: str | None = None
    history_epoch: int = Field(ge=0)
    turn_id: str
    producer: ToolCallProducer
    request: ToolCallRequest
    accepted_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.origin_session_id is None:
            self.origin_session_id = self.session_id
        if self.request.id != self.identity.tool_call_id:
            raise ValueError("Call request identity does not match its native identity")
        return self


class ToolCallStart(VersionedOuroModel):
    """The authorized binding and actual parameters fixed before entering a tool."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    identity: ToolCallIdentity
    attempt: int = Field(ge=1)
    binding: ToolInvocationBinding
    # None reuses the immutable original arguments, including an original empty object.
    arguments_override: dict[str, Any] | None = None
    started_at: datetime = Field(default_factory=utc_now)

    @field_validator("binding", mode="before")
    @classmethod
    def require_parameter_binding(cls, value: object) -> object:
        if isinstance(value, ToolInvocation):
            raise ValueError("use from_invocation to bind execution arguments to their Call")
        return value

    @model_validator(mode="after")
    def validate_binding_identity(self) -> Self:
        if (
            self.binding.run_id != self.identity.run_id
            or self.binding.tool_call_id != self.identity.tool_call_id
        ):
            raise ValueError("Call start has a foreign execution binding")
        return self

    @classmethod
    def from_invocation(
        cls, call: ToolCallRecord, invocation: ToolInvocation, *, attempt: int
    ) -> ToolCallStart:
        result = cls(
            session_id=call.session_id,
            identity=call.identity,
            attempt=attempt,
            binding=ToolInvocationBinding.model_validate(
                invocation.model_dump(exclude={"arguments"})
            ),
            arguments_override=(
                None
                if invocation.arguments == call.request.arguments
                else deepcopy(invocation.arguments)
            ),
        )
        result.validate_call(call)
        return result

    def validate_call(self, call: ToolCallRecord) -> None:
        if (
            call.session_id != self.session_id
            or call.identity != self.identity
            or call.request.name != self.binding.tool_name
            or call.origin_session_id != self.binding.session_id
            or call.producer != self.binding.producer
            or call.turn_id != self.binding.turn_id
        ):
            raise ValueError("Call start does not belong to the accepted Call")
        if self.arguments_override == call.request.arguments:
            raise ValueError("unchanged execution arguments must reference the accepted Call")

    def materialize(self, call: ToolCallRecord) -> ToolInvocation:
        self.validate_call(call)
        return ToolInvocation.model_validate(
            {
                **self.binding.model_dump(),
                "arguments": deepcopy(
                    self.arguments_override
                    if self.arguments_override is not None
                    else call.request.arguments
                ),
            }
        )


class ToolOutcomeRecord(VersionedOuroModel):
    """One certain observation, independently of its eventual history publication."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 2

    session_id: str
    identity: ToolCallIdentity
    # Zero is a certain preflight rejection; positive attempts require a durable start.
    attempt: int = Field(ge=0)
    value: ToolResultValue
    observed_at: datetime = Field(default_factory=utc_now)

    @field_validator("value", mode="before")
    @classmethod
    def separate_proposals(cls, value: object) -> object:
        if isinstance(value, ToolResult):
            if value.harness_change or value.session_control_transitions:
                raise ValueError(
                    "pending business proposals must be accepted separately from Outcome content"
                )
            return value.model_dump(exclude={"harness_change", "session_control_transitions"})
        return value

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.value.tool_call_id != self.identity.tool_call_id:
            raise ValueError("Outcome identity does not match its Call")
        if self.attempt == 0 and not self.value.is_error:
            raise ValueError("a preflight Outcome must describe a rejection")
        return self

    def validate_call(self, call: ToolCallRecord) -> None:
        if (
            call.session_id != self.session_id
            or call.identity != self.identity
            or call.request.name != self.value.tool_name
        ):
            raise ValueError("Outcome does not belong to the accepted Call")

    def as_fact(self) -> ToolOutcomeRecord:
        if isinstance(self.value.content, str):
            return self
        return self.model_copy(
            update={
                "value": self.value.model_copy(
                    update={
                        "content": [
                            media_reference(block) if isinstance(block, MediaContent) else block
                            for block in self.value.content
                        ]
                    }
                )
            }
        )

    def validate_stored_content(self) -> None:
        if isinstance(self.value.content, str):
            return
        if any(
            isinstance(block, MediaContent)
            and block.uri
            and (block.resource_id is not None or media_data_uri(block.uri) is not None)
            for block in self.value.content_blocks
        ):
            raise ToolFactConflict("stored Outcome media must reference its Resource")


class ToolFactConflict(PersistenceError):
    """A Call identity or its immutable execution facts have conflicting values."""


class ToolCallFacts(OuroModel):
    """An explicit set of Call facts for a native write or a complete archive."""

    calls: tuple[ToolCallRecord, ...] = ()
    starts: tuple[ToolCallStart, ...] = ()
    outcomes: tuple[ToolOutcomeRecord, ...] = ()

    @model_validator(mode="after")
    def validate_unique_identities(self) -> Self:
        for identities in (
            [call.identity for call in self.calls],
            [(start.identity, start.attempt) for start in self.starts],
            [(outcome.identity, outcome.attempt) for outcome in self.outcomes],
        ):
            if len(identities) != len(set(identities)):
                raise ValueError("duplicate Call fact identity")
        return self

    @property
    def records(self) -> tuple[ToolCallRecord | ToolCallStart | ToolOutcomeRecord, ...]:
        return (*self.calls, *self.starts, *self.outcomes)

    @property
    def call_identities(self) -> tuple[ToolCallIdentity, ...]:
        return tuple(dict.fromkeys(item.identity for item in self.records))

    @property
    def execution_run_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                [call.identity.run_id for call in self.calls]
                + [start.identity.run_id for start in self.starts]
            )
        )

    @property
    def start_keys(self) -> tuple[tuple[ToolCallIdentity, int], ...]:
        return tuple(
            dict.fromkeys(
                [(item.identity, item.attempt) for item in self.starts]
                + [(item.identity, item.attempt - 1) for item in self.starts if item.attempt > 1]
                + [(item.identity, max(1, item.attempt)) for item in self.outcomes]
            )
        )

    @property
    def outcome_keys(self) -> tuple[tuple[ToolCallIdentity, int], ...]:
        return tuple(
            dict.fromkeys(
                [(item.identity, item.attempt) for item in self.outcomes]
                + [(item.identity, item.attempt - 1) for item in self.starts]
            )
        )

    def prepare(
        self,
        *,
        session_id: str,
        history_epoch: int,
        active_runs: Mapping[str, int],
        existing: ToolCallFacts,
    ) -> ToolCallFacts:
        calls = {call.identity: call for call in existing.calls}
        starts = {(start.identity, start.attempt): start for start in existing.starts}
        outcomes = {(outcome.identity, outcome.attempt): outcome for outcome in existing.outcomes}
        fresh_calls = []
        fresh_starts = []
        fresh_outcomes = []
        for accepted in self.calls:
            if accepted.session_id != session_id:
                raise ToolFactConflict("foreign Call Session")
            previous = calls.get(accepted.identity)
            if previous is not None:
                if previous != accepted:
                    raise ToolFactConflict("Call identity already has different accepted content")
                continue
            if (
                accepted.history_epoch != history_epoch
                or accepted.origin_session_id != session_id
                or active_runs.get(accepted.identity.run_id) != history_epoch
            ):
                raise ToolFactConflict("Call acceptance requires its current active Run")
            calls[accepted.identity] = accepted
            fresh_calls.append(accepted)
        for start in self.starts:
            call = calls.get(start.identity)
            if call is None:
                raise ToolFactConflict("Call start has no accepted Call")
            try:
                start.validate_call(call)
            except ValueError as exc:
                raise ToolFactConflict(str(exc)) from exc
            key = (start.identity, start.attempt)
            previous_start = starts.get(key)
            if previous_start is not None:
                if previous_start != start:
                    raise ToolFactConflict("Call start already has different execution parameters")
                continue
            if (
                call.session_id != session_id
                or call.origin_session_id != session_id
                or call.history_epoch != history_epoch
                or active_runs.get(call.identity.run_id) != history_epoch
            ):
                raise ToolFactConflict("starting a Call requires its current active Run")
            if start.attempt > 1 and (start.identity, start.attempt - 1) not in starts:
                raise ToolFactConflict("Call attempt has no preceding execution")
            if (start.identity, start.attempt - 1) in outcomes:
                raise ToolFactConflict("a certain Call outcome cannot be executed again")
            starts[key] = start
            fresh_starts.append(start)
        for outcome in self.outcomes:
            call = calls.get(outcome.identity)
            if call is None:
                raise ToolFactConflict("Outcome has no accepted Call")
            try:
                outcome.validate_call(call)
            except ValueError as exc:
                raise ToolFactConflict(str(exc)) from exc
            if call.session_id != session_id:
                raise ToolFactConflict("foreign Outcome Session")
            key = (outcome.identity, outcome.attempt)
            if outcome.attempt > 0 and key not in starts:
                raise ToolFactConflict("Outcome has no durable Call start")
            previous_outcome = outcomes.get(key)
            if previous_outcome is not None:
                if previous_outcome != outcome:
                    raise ToolFactConflict("a certain Outcome cannot be replaced")
                continue
            if call.origin_session_id != session_id:
                raise ToolFactConflict(
                    "a copied historical Call cannot receive new execution facts"
                )
            if outcome.attempt == 0 and (outcome.identity, 1) in starts:
                raise ToolFactConflict("a started Call cannot receive a preflight rejection")
            outcomes[key] = outcome
            fresh_outcomes.append(outcome)
        return ToolCallFacts(
            calls=tuple(fresh_calls), starts=tuple(fresh_starts), outcomes=tuple(fresh_outcomes)
        )

    def validate_snapshot(
        self, session_id: str, *, run_epochs: Mapping[str, int] | None = None
    ) -> None:
        for outcome in self.outcomes:
            outcome.validate_stored_content()
        self.prepare(session_id=session_id, history_epoch=0, active_runs={}, existing=self)
        starts = {(start.identity, start.attempt) for start in self.starts}
        if any(
            start.attempt > 1 and (start.identity, start.attempt - 1) not in starts
            for start in self.starts
        ):
            raise ToolFactConflict("Call snapshot is missing a preceding attempt")
        outcomes = {(outcome.identity, outcome.attempt) for outcome in self.outcomes}
        if any((start.identity, start.attempt - 1) in outcomes for start in self.starts):
            raise ToolFactConflict("Call snapshot executes after a certain Outcome")
        if run_epochs is not None and any(
            call.origin_session_id == session_id
            and run_epochs.get(call.identity.run_id) != call.history_epoch
            for call in self.calls
        ):
            raise ToolFactConflict("Call snapshot is missing its owning Run")
