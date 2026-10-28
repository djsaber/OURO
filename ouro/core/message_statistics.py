"""Small projections of published messages, never owners of their content."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import Field, model_validator

from ouro.core.base import OuroModel
from ouro.core.message_roles import MessageRole
from ouro.core.messages import MessageRecord

InputOrigin = Literal["automatic", "other"]


class RunResultReferences(OuroModel):
    initial_message_id: str | None = None
    result_message_id: str | None = None
    resource_refs: tuple[str, ...] = ()


def delegation_input_id(message: MessageRecord) -> str | None:
    identity = message.metadata.get("delegation_id")
    return (
        identity
        if message.role == "user"
        and message.metadata.get("session_input_source") == "delegation"
        and isinstance(identity, str)
        else None
    )


def collect_run_result_references(
    records: Iterable[tuple[str, MessageRole, str | None, Sequence[str]]],
    *,
    delegation_id: str,
) -> RunResultReferences:
    initial = result = None
    resources: dict[str, None] = {}
    for message_id, role, source, resource_refs in records:
        if initial is None and role == "user" and source == delegation_id:
            initial = message_id
        if role == "assistant":
            result = message_id
        if role in {"assistant", "tool_result"}:
            resources.update(dict.fromkeys(resource_refs))
    return RunResultReferences(
        initial_message_id=initial, result_message_id=result, resource_refs=tuple(resources)
    )


class RunMessageCounts(OuroModel):
    run_id: str
    tool_call_count: int = Field(default=0, ge=0)
    input_count: int = Field(default=0, ge=0)
    automatic_input_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> RunMessageCounts:
        if self.automatic_input_count > self.input_count:
            raise ValueError("automatic inputs cannot exceed consumed inputs")
        return self

    @property
    def automatic_continuation(self) -> bool | None:
        return self.automatic_input_count == self.input_count if self.input_count else None


def message_input_origin(message: MessageRecord) -> InputOrigin | None:
    if "session_input_mode" not in message.metadata:
        return None
    return "automatic" if message.metadata.get("continuation_decision_id") is not None else "other"


def message_call_keys(message: MessageRecord) -> tuple[str, ...]:
    if message.run_id is None:
        return ()
    # A request and its result count once. Provider call IDs may repeat in another turn.
    return tuple(
        dict.fromkeys(
            json.dumps(
                (message.run_id, message.turn_id, identity.tool_call_id), separators=(",", ":")
            )
            for identity in message.referenced_call_ids
        )
    )
