from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from ouro.core.base import OuroModel, VersionedOuroModel
from ouro.core.errors import PersistenceError
from ouro.core.input_content import InputContent, InputFile
from ouro.core.message_roles import MessageRole
from ouro.core.message_semantics import SemanticTag
from ouro.core.messages import MessageRecord, SkillActivationContent, TextContent
from ouro.core.run_drivers import RunDriverBinding
from ouro.core.session_controls import SessionInputMode

QueuedInputMode = SessionInputMode
QueuedInputSource = Literal["user", "host", "plugin", "delegation"]


class QueuedInput(OuroModel):
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
    scaffold: bool
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
            not self.prompt.strip()
            and not self.skill_activations
            and not self.resource_refs
            and not self.attachments
        ):
            raise ValueError("queued input requires a prompt or skill activation")
        if self.skill_activations:
            if self.source != "user" or self.role != "user" or self.scaffold:
                raise ValueError("skill activation is only valid on direct user input")
            if self.mode != "follow_up":
                raise ValueError("skill activation cannot steer an active run")
        return self


class InputRecord(VersionedOuroModel):
    """Immutable accepted intent, including its fixed attachment and skill units."""

    session_id: str
    item: QueuedInput


def resolve_input_content(fact: MessageRecord, record: InputRecord | None = None) -> dict[str, Any]:
    """Resolve only this message's fixed Input units, with no providers or file reads."""
    reference = fact.input_ref
    values: dict[str, Any] = {}
    if reference is not None:
        if (
            record is None
            or record.session_id != fact.session_id
            or record.item.queue_id != reference.input_id
        ):
            raise PersistenceError("message references a missing or foreign Input")
        item = record.item
        if reference.part == "body":
            if (fact.role, fact.scaffold, fact.semantic_tag) != (
                item.role,
                item.scaffold,
                item.semantic_tag,
            ):
                raise PersistenceError("message semantics do not match its accepted Input body")
            values["content"] = list(item.skill_activations)
            if item.prompt:
                values["content"].append(TextContent(text=item.prompt))
        elif item.source != "user" or (fact.role, fact.scaffold, fact.semantic_tag) != (
            "user",
            True,
            "attachments",
        ):
            raise PersistenceError("attachment observation does not match its accepted Input")
        if reference.attachments_from_input:
            values["input_attachments"] = tuple(
                attachment.model_copy(update={"text_offset": 0})
                if reference.part == "attachments"
                else attachment
                for attachment in item.attachments
            )
    return values


class InputState(VersionedOuroModel):
    """Small lifecycle state; terminal states retain the original acceptance."""

    session_id: str
    input_id: str = Field(min_length=1)
    history_epoch: int = Field(ge=0)
    history_bound: bool
    mode: QueuedInputMode
    source: QueuedInputSource
    role: MessageRole
    scaffold: bool
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
        fields = {
            name: getattr(record.item, name)
            for name in (
                "history_epoch",
                "history_bound",
                "mode",
                "source",
                "role",
                "scaffold",
                "semantic_tag",
                "coalesce_key",
                "plugin_id",
                "contribution_id",
                "session_control_controller_id",
                "session_control_activation_id",
            )
        }
        return cls(session_id=record.session_id, input_id=record.item.queue_id, **fields)

    def matches_intent(self, record: InputRecord) -> bool:
        excluded = {"status", "message_ids"}
        return self.model_dump(exclude=excluded) == self.pending(record).model_dump(
            exclude=excluded
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
    identities = {record.item.queue_id for record in inputs}
    if len(identities) != len(inputs) or any(record.session_id != session_id for record in inputs):
        raise ValueError("duplicate or foreign accepted input")
    actual = (
        states if states is not None else tuple(InputState.pending(record) for record in inputs)
    )
    if (
        {state.input_id for state in actual} != identities
        or len(actual) != len(identities)
        or any(state.session_id != session_id for state in actual)
        or {state.input_id for state in actual if state.status == "pending"} != set(pending_ids)
    ):
        raise ValueError("accepted inputs and lifecycle states do not match the Session queue")
    by_id = {record.item.queue_id: record for record in inputs}
    states_by_id = {state.input_id: state for state in actual}
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
        resolve_input_content(message.as_fact(), by_id[identity])
    message_inputs = {message.message_id: message.input_id for message in messages}
    for state in actual:
        if not state.matches_intent(by_id[state.input_id]):
            raise ValueError("input lifecycle routing does not match its accepted intent")
        if state.status == "consumed" and any(
            message_inputs.get(identity) != state.input_id for identity in state.message_ids
        ):
            raise ValueError("consumed input references a missing or foreign message")
    return actual
