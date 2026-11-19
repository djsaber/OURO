from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from ouro.core.base import OuroModel, VersionedOuroModel
from ouro.core.errors import PersistenceError
from ouro.core.input_content import InputContent, InputFile
from ouro.core.message_roles import MessageRole
from ouro.core.message_semantics import MessageSemantics, SemanticTag
from ouro.core.messages import MessageRecord, SkillActivationContent, TextContent
from ouro.core.run_drivers import RunDriverBinding
from ouro.core.session_controls import SessionInputMode

QueuedInputMode = SessionInputMode
QueuedInputSource = Literal["user", "host", "plugin", "delegation"]

_ROUTING_FIELDS = (
    "history_epoch",
    "history_bound",
    "mode",
    "source",
    "role",
    "semantic_tag",
    "coalesce_key",
    "plugin_id",
    "contribution_id",
    "session_control_controller_id",
    "session_control_activation_id",
)


class QueuedInput(MessageSemantics):
    history_epoch: int = Field(default=0, ge=0)
    history_bound: bool = False
    queue_id: str = Field(min_length=1)
    prompt: str = ""
    skill_activations: tuple[SkillActivationContent, ...] = ()
    mode: QueuedInputMode
    driver: RunDriverBinding | None = None
    title: str | None = None
    resource_refs: tuple[str, ...] = ()
    attachments: tuple[InputFile, ...] = Field(default=(), max_length=16)
    queued_at: datetime
    source: QueuedInputSource
    role: MessageRole
    semantic_tag: SemanticTag | None
    batch_id: str | None = None
    batch_index: int | None = Field(default=None, ge=0)
    batch_size: int | None = Field(default=None, ge=1)
    coalesce_key: str | None = Field(default=None, min_length=1, max_length=128)
    host_source_id: str | None = None
    delegation_id: str | None = None
    plugin_id: str | None = None
    contribution_id: str | None = None
    generation_id: str | None = None
    execution_contract_hash: str | None = None
    caused_by_run_id: str | None = None
    continuation_decision_id: str | None = None
    session_control_controller_id: str | None = None
    session_control_activation_id: str | None = None
    prompt_policy_checked: bool = False
    prospective_run_id: str | None = None
    extension_catalog_id: str | None = None
    extension_generations: tuple[dict[str, object], ...] = ()

    @field_validator("host_source_id")
    @classmethod
    def normalize_host_source_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_source(self) -> QueuedInput:
        if self.driver is not None and self.mode != "follow_up":
            raise ValueError("Driver selection cannot steer an existing Run")
        plugin_attribution = (
            self.plugin_id,
            self.contribution_id,
            self.generation_id,
            self.execution_contract_hash,
            self.continuation_decision_id,
            self.session_control_controller_id,
            self.session_control_activation_id,
        )
        if self.source == "delegation" and not self.delegation_id:
            raise ValueError("delegated task input requires a delegation id")
        if self.source != "delegation" and self.delegation_id is not None:
            raise ValueError("only delegated task input can carry a delegation id")
        if self.source == "plugin" and (
            not self.plugin_id
            or not self.contribution_id
            or not self.generation_id
            or not self.execution_contract_hash
        ):
            raise ValueError("plugin queued input requires complete extension attribution")
        if self.source == "plugin" and self.host_source_id is not None:
            raise ValueError("plugin queued input cannot carry Host attribution")
        if self.source == "delegation" and (
            self.host_source_id is not None
            or any(value is not None for value in plugin_attribution)
        ):
            raise ValueError("delegated task input cannot carry Host or plugin attribution")
        if self.source == "host":
            if not self.host_source_id:
                raise ValueError("Host queued input requires a source id")
            if any(value is not None for value in plugin_attribution):
                raise ValueError("Host queued input cannot carry plugin attribution")
        if self.source == "user" and (
            self.host_source_id is not None
            or any(value is not None for value in plugin_attribution)
        ):
            raise ValueError("user queued input cannot carry Host or plugin attribution")
        batch_fields = (self.batch_id, self.batch_index, self.batch_size)
        if any(value is not None for value in batch_fields) and any(
            value is None for value in batch_fields
        ):
            raise ValueError("queued input batch identity requires id, index, and size")
        if (
            self.batch_id is not None
            and self.batch_index is not None
            and self.batch_size is not None
        ):
            if self.batch_index >= self.batch_size:
                raise ValueError("queued input batch index must be smaller than its size")
        if self.coalesce_key is not None and self.batch_id is None:
            raise ValueError("coalescing is only valid for an atomic input unit")
        if self.source == "plugin" and self.resource_refs:
            raise ValueError("plugin queued input cannot carry Host resource references")
        if (self.session_control_controller_id is None) is not (
            self.session_control_activation_id is None
        ):
            raise ValueError(
                "queued input Session control binding requires controller and activation ids"
            )
        if self.prompt_policy_checked and self.source != "user":
            raise ValueError("only direct user input can be prompt-policy checked")
        if self.prompt_policy_checked and not self.prospective_run_id:
            raise ValueError("prompt-policy checked input requires a prospective run id")
        if self.extension_generations and not self.extension_catalog_id:
            raise ValueError("queued extension generations require a catalog id")
        if self.attachments:
            content = InputContent(text=self.prompt, attachments=self.attachments)
            if content.resource_refs != self.resource_refs:
                raise ValueError("Attachment identities must match resource references")
        if (
            not self.semantic_attributes
            and not self.prompt.strip()
            and not self.skill_activations
            and not self.resource_refs
            and not self.attachments
        ):
            raise ValueError("queued input requires content or semantic attributes")
        if self.skill_activations and (self.source != "user" or self.role != "user"):
            raise ValueError("skill activation is only valid on direct user input")
        return self


class InputRecord(VersionedOuroModel):
    """Immutable accepted intent, including its fixed attachment and skill units."""

    schema_version: Literal[2] = 2

    session_id: str
    item: QueuedInput


def validate_input_message_semantics(
    message: MessageRecord, intent: QueuedInput | InputState
) -> None:
    reference = message.input_ref
    if reference is None:
        return
    semantics = message.role, message.semantic_tag
    if reference.part == "body":
        if semantics != (intent.role, intent.semantic_tag):
            raise PersistenceError("message semantics do not match its accepted Input body")
    elif intent.source != "user" or semantics != ("harness", "attachments"):
        raise PersistenceError("attachment observation does not match its accepted Input")


def _bound_item(fact: MessageRecord, record: InputRecord | None) -> QueuedInput:
    if (
        record is None
        or record.session_id != fact.session_id
        or record.item.queue_id != fact.input_id
    ):
        raise PersistenceError("message references a missing or foreign Input")
    validate_input_message_semantics(fact, record.item)
    return record.item


def resolve_input_content(fact: MessageRecord, record: InputRecord | None = None) -> dict[str, Any]:
    """Build independent display units without copying the accepted execution contract."""
    reference = fact.input_ref
    if reference is None:
        return {}
    item = _bound_item(fact, record)
    values: dict[str, Any] = {}
    if reference.part == "body":
        values["semantic_attributes"] = item.semantic_attributes.copy()
        values["content"] = [skill.model_copy() for skill in item.skill_activations]
        if item.prompt:
            values["content"].append(TextContent(text=item.prompt))
    if reference.attachments_from_input:
        offsets = {"text_offset": 0} if reference.part == "attachments" else None
        values["input_attachments"] = tuple(
            file.model_copy(update=offsets) for file in item.attachments
        )
    return values


class InputState(VersionedOuroModel):
    """Small lifecycle state; terminal states retain the original acceptance."""

    schema_version: Literal[2] = 2

    session_id: str
    input_id: str = Field(min_length=1)
    history_epoch: int = Field(ge=0)
    history_bound: bool
    mode: QueuedInputMode
    source: QueuedInputSource
    role: MessageRole
    semantic_tag: SemanticTag | None
    coalesce_key: str | None = None
    plugin_id: str | None = None
    contribution_id: str | None = None
    session_control_controller_id: str | None = None
    session_control_activation_id: str | None = None
    status: Literal["pending", "consumed", "cancelled", "withdrawn"] = "pending"
    message_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_lifecycle(self) -> InputState:
        if (self.status in {"consumed", "withdrawn"}) != bool(self.message_ids):
            raise ValueError("consumed or withdrawn inputs must retain their message identities")
        if len(set(self.message_ids)) != len(self.message_ids):
            raise ValueError("input state contains duplicate message identities")
        return self

    @classmethod
    def pending(cls, record: InputRecord) -> InputState:
        fields = {name: getattr(record.item, name) for name in _ROUTING_FIELDS}
        return cls(session_id=record.session_id, input_id=record.item.queue_id, **fields)

    def matches_intent(self, record: InputRecord) -> bool:
        return (
            self.session_id == record.session_id
            and self.input_id == record.item.queue_id
            and all(getattr(self, name) == getattr(record.item, name) for name in _ROUTING_FIELDS)
        )


class InputStateChange(OuroModel):
    input_id: str = Field(min_length=1)
    status: Literal["consumed", "cancelled"]
    message_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_messages(self) -> InputStateChange:
        if (self.status == "consumed") != bool(self.message_ids):
            raise ValueError("only input consumption specifies newly committed messages")
        if len(set(self.message_ids)) != len(self.message_ids):
            raise ValueError("input consumption contains duplicate message identities")
        return self


def validate_input_snapshot(
    session_id: str,
    pending_ids: tuple[str, ...],
    inputs: tuple[InputRecord, ...],
    states: tuple[InputState, ...] | None,
    *,
    messages: Sequence[MessageRecord],
) -> tuple[InputState, ...]:
    by_id = {record.item.queue_id: record for record in inputs}
    if len(by_id) != len(inputs) or any(record.session_id != session_id for record in inputs):
        raise ValueError("duplicate or foreign accepted input")
    actual = (
        states if states is not None else tuple(InputState.pending(record) for record in inputs)
    )
    states_by_id = {state.input_id: state for state in actual}
    if (
        states_by_id.keys() != by_id.keys()
        or len(actual) != len(by_id)
        or any(state.session_id != session_id for state in actual)
        or {state.input_id for state in actual if state.status == "pending"} != set(pending_ids)
    ):
        raise ValueError("accepted inputs and lifecycle states do not match the Session queue")
    for message in messages:
        identity = message.input_id
        if identity is None:
            continue
        state = states_by_id.get(identity)
        if (
            state is None
            or state.status not in {"consumed", "withdrawn"}
            or message.message_id not in state.message_ids
        ):
            raise ValueError("message does not belong to a consumed Input")
        _bound_item(message, by_id[identity])
    message_inputs = {message.message_id: message.input_id for message in messages}
    for state in actual:
        if not state.matches_intent(by_id[state.input_id]):
            raise ValueError("input lifecycle routing does not match its accepted intent")
        if state.status == "consumed" and any(
            message_inputs.get(identity) != state.input_id for identity in state.message_ids
        ):
            raise ValueError("consumed input references a missing or foreign message")
    return actual
