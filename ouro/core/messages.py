from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from ouro.core.base import Metadata, MetadataField, OuroModel, VersionedOuroModel, new_id, utc_now
from ouro.core.content import ImageContent as ImageContent
from ouro.core.content import MediaContent, media_reference, media_resource_ids
from ouro.core.content import TextContent as TextContent
from ouro.core.input_content import InputContent, InputFile
from ouro.core.message_roles import MessageRole, RunMessageKind, run_message_kind
from ouro.core.message_semantics import (
    MessageSemantics,
    SemanticAttributes,
    SemanticTag,
    escape_semantic_envelope_delimiters,
    render_semantic_envelope,
)
from ouro.core.tool_facts import ToolCallIdentity
from ouro.core.tools import ToolCallProducer, ToolCallRequest, ToolResultFeedback

SkillActivationSource = Literal["user", "workspace", "plugin"]


class SkillActivationContent(OuroModel):
    """Durable user-selected skill instructions before Provider projection."""

    type: Literal["skill_activation"] = "skill_activation"
    name: str = Field(min_length=1)
    text_offset: int = Field(default=0, ge=0)
    order: int = Field(default=0, ge=0)
    source: SkillActivationSource
    location: str = Field(min_length=1)
    base_directory: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("name", "location", "base_directory", "instructions")
    @classmethod
    def require_non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("skill activation fields must not be blank")
        return value

    def render(self) -> str:
        attributes = {
            "name": self.name,
            "source": self.source,
            "location": self.location,
            "base-directory": self.base_directory,
            "digest": self.content_digest,
        }
        instructions = render_semantic_envelope(
            self.instructions, "ouro-skill-activation", attributes
        )
        return (
            "The user explicitly activated this skill. Follow its instructions for this "
            "request.\n\n"
            f"{instructions}\n\n"
        )


ContentBlock = Annotated[
    TextContent | MediaContent | SkillActivationContent,
    Field(discriminator="type"),
]


class ToolResultReference(OuroModel):
    tool_call_id: str
    tool_name: str
    is_error: bool = False
    producer: ToolCallProducer = Field(default_factory=ToolCallProducer)


class ProviderContinuationArtifact(OuroModel):
    """Protocol replay data, separate from canonical conversation facts.

    Core persists opaque blocks; the target codec decides replay compatibility.
    Incompatible routes continue from canonical content and complete tool frames.
    Retained response data is independent of its Run's execution authority.
    """

    provider_id: str
    protocol: str
    model_id: str
    blocks: tuple[dict[str, Any], ...]

    @model_validator(mode="after")
    def _validate_identity_and_blocks(self) -> ProviderContinuationArtifact:
        if not self.provider_id.strip() or not self.protocol.strip() or not self.model_id.strip():
            raise ValueError("provider continuation identity must not be empty")
        if not self.blocks:
            raise ValueError("provider continuation must contain at least one opaque block")
        return self

    def matches(self, *, provider_id: str, protocol: str, model_id: str) -> bool:
        return (
            self.provider_id == provider_id
            and self.protocol == protocol
            and self.model_id == model_id
        )


class ModelResponseRecord(OuroModel):
    """Route and timing facts committed atomically with one assistant message."""

    provider_id: str = Field(min_length=1)
    endpoint_id: str | None = None
    model_id: str = Field(min_length=1)
    started_at: datetime
    ended_at: datetime
    reasoning_ended_at: datetime | None = None

    @model_validator(mode="after")
    def validate_timing(self) -> ModelResponseRecord:
        if self.endpoint_id is not None and not self.endpoint_id.strip():
            raise ValueError("model response endpoint_id must not be blank")
        if self.ended_at < self.started_at:
            raise ValueError("model response ended_at cannot precede started_at")
        if self.reasoning_ended_at is not None and not (
            self.started_at <= self.reasoning_ended_at <= self.ended_at
        ):
            raise ValueError("model response reasoning_ended_at must be within the request")
        return self


class ToolExecutionTiming(OuroModel):
    """Durable execution timing attached to a canonical tool-result message."""

    started_at: datetime
    ended_at: datetime

    @model_validator(mode="after")
    def validate_timing(self) -> ToolExecutionTiming:
        if self.ended_at < self.started_at:
            raise ValueError("tool execution ended_at cannot precede started_at")
        return self


class ModelMessage(MessageSemantics):
    """Provider-neutral message containing only model-visible semantics.

    Canonical ``MessageRecord`` identity, persistence metadata and resource
    references deliberately stop at the context projection boundary.
    """

    role: MessageRole
    content: list[ContentBlock] = Field(default_factory=list)
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    tool_result: ToolResultReference | None = None
    reasoning_content: str = ""
    provider_continuation: ProviderContinuationArtifact | None = None

    @model_validator(mode="after")
    def validate_tool_fields(self) -> ModelMessage:
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant model messages may contain tool_calls")
        if self.tool_result is not None and self.role != "tool_result":
            raise ValueError("only tool_result model messages may contain tool_result")
        if (self.reasoning_content or self.provider_continuation is not None) and (
            self.role != "assistant"
        ):
            raise ValueError("only assistant model messages may contain reasoning state")
        _validate_skill_activation_blocks(self.role, self.content)
        return self

    @classmethod
    def from_record(cls, message: MessageRecord) -> ModelMessage:
        return cls.model_validate(message, from_attributes=True)

    @classmethod
    def text(
        cls,
        *,
        role: MessageRole,
        text: str,
        semantic_tag: SemanticTag | None = None,
        semantic_attributes: SemanticAttributes | None = None,
    ) -> ModelMessage:
        return cls(
            role=role,
            content=[TextContent(text=text)],
            semantic_tag=semantic_tag,
            semantic_attributes=semantic_attributes or {},
        )

    @property
    def text_content(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, TextContent))


class InputMessageReference(OuroModel):
    """A fixed accepted body or its attachment observation, never a JSON projection."""

    input_id: str = Field(min_length=1)
    part: Literal["body", "attachments"] = "body"
    attachments_from_input: bool = True


class ToolMessageReference(OuroModel):
    """A fixed Outcome or the Call explained by this message's own feedback."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: ToolCallIdentity
    outcome_attempt: int | None = Field(default=None, ge=0)
    feedback_is_error: bool | None = None

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if (self.outcome_attempt is None) != (self.feedback_is_error is not None):
            raise ValueError(
                "tool message must select either a certain Outcome or literal feedback"
            )
        return self


class MessageRecord(VersionedOuroModel, MessageSemantics):
    """One explicitly materialized message for execution or a selected history read.

    Persistence uses ``as_fact``; resolving an Input never changes its content owner.
    This value has no loader, lazy collection, or implicit storage access.
    """

    schema_version: Literal[9] = 9
    message_id: str = Field(default_factory=lambda: new_id("msg"))
    before_harness_version: str | None = None
    session_id: str
    run_id: str | None = None
    turn_id: str | None = None
    role: MessageRole
    created_at: datetime = Field(default_factory=utc_now)
    sequence: int = 0
    content: list[ContentBlock] = Field(default_factory=list)
    resource_refs: list[str] = Field(default_factory=list)
    input_attachments: tuple[InputFile, ...] = ()
    input_ref: InputMessageReference | None = None
    call_refs: tuple[ToolCallIdentity, ...] = ()
    outcome_ref: ToolMessageReference | None = None
    tool_feedback: ToolResultFeedback | None = None
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    tool_result: ToolResultReference | None = None
    reasoning_content: str = ""
    model_response: ModelResponseRecord | None = None
    tool_execution: ToolExecutionTiming | None = None
    provider_continuation: ProviderContinuationArtifact | None = None
    metadata: Metadata = MetadataField

    @model_validator(mode="after")
    def validate_message(self) -> MessageRecord:
        return self.validate_tool_fields()

    def validate_tool_fields(self) -> MessageRecord:
        reserved_metadata = {
            "tool_calls",
            "tool_call_id",
            "tool_name",
            "is_error",
            "reasoning_content",
            "model_response",
            "tool_execution",
            "provider_continuation",
            "semantic_tag",
            "semantic_attributes",
            "queued_input_id",
        }
        misplaced = sorted(reserved_metadata.intersection(self.metadata))
        if misplaced:
            fields = ", ".join(misplaced)
            raise ValueError(
                f"structured state must use typed message fields, not metadata: {fields}"
            )
        if self.turn_id is not None and self.run_id is None:
            raise ValueError("message turn_id requires run_id")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages may contain tool_calls")
        if self.call_refs and self.role != "assistant":
            raise ValueError("only assistant messages may reference original Calls")
        if len(set(self.call_refs)) != len(self.call_refs):
            raise ValueError("assistant message contains duplicate Call references")
        if self.outcome_ref is not None and self.role != "tool_result":
            raise ValueError("only tool-result messages may reference an Outcome or feedback")
        if self.tool_feedback is not None and (
            self.outcome_ref is None or self.outcome_ref.outcome_attempt is None
        ):
            raise ValueError("tool settlement feedback requires a fixed Outcome reference")
        if self.tool_result is not None and self.role != "tool_result":
            raise ValueError("only tool_result messages may contain tool_result")
        if (self.reasoning_content or self.provider_continuation is not None) and (
            self.role != "assistant"
        ):
            raise ValueError("only assistant messages may contain reasoning state")
        if self.model_response is not None and self.role != "assistant":
            raise ValueError("only assistant messages may contain model response state")
        if self.tool_execution is not None and (
            self.role != "tool_result" or (self.tool_result is None and self.outcome_ref is None)
        ):
            raise ValueError("tool execution timing requires a tool_result message")
        _validate_skill_activation_blocks(self.role, self.content)
        if self.input_attachments:
            if self.role not in {"user", "harness"}:
                raise ValueError("input attachments require a user or harness message")
            # Offsets into a referenced body are checked when that body is materialized.
            if not (
                self.input_ref is not None and self.input_ref.part == "body" and not self.content
            ):
                InputContent(
                    text="" if self.semantic_tag == "attachments" else self.text_content,
                    attachments=self.input_attachments,
                )
        return self

    @classmethod
    def text(
        cls,
        *,
        session_id: str,
        role: MessageRole,
        text: str,
        run_id: str | None = None,
        turn_id: str | None = None,
        sequence: int = 0,
        semantic_tag: SemanticTag | None = None,
        semantic_attributes: SemanticAttributes | None = None,
    ) -> MessageRecord:
        return cls(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            role=role,
            sequence=sequence,
            semantic_tag=semantic_tag,
            semantic_attributes=semantic_attributes or {},
            content=[TextContent(text=text)],
        )

    @property
    def text_content(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, TextContent))

    @property
    def skill_activations(self) -> tuple[SkillActivationContent, ...]:
        return tuple(block for block in self.content if isinstance(block, SkillActivationContent))

    @property
    def input_id(self) -> str | None:
        return self.input_ref.input_id if self.input_ref is not None else None

    @property
    def run_message_kind(self) -> RunMessageKind:
        return run_message_kind(
            self.role,
            supplied=self.input_ref is not None
            or self.metadata.get("context_provider_delivery_key") is not None,
        )

    @property
    def referenced_call_ids(self) -> tuple[ToolCallIdentity, ...]:
        return self.call_refs or ((self.outcome_ref.identity,) if self.outcome_ref else ())

    @property
    def referenced_resource_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([*self.resource_refs, *media_resource_ids(self.content)]))

    def validate_persisted_content(self) -> Self:
        """Check content ownership after decoding a stored message."""
        if any(
            isinstance(block, MediaContent) and block.resource_id is not None and block.uri
            for block in self.content
        ):
            raise ValueError("media resource bytes cannot also be owned by a message")
        if self.tool_calls:
            raise ValueError("original tool parameters belong to Call facts")
        if self.tool_result is not None:
            raise ValueError("tool result identity belongs to its Call reference")
        if self.outcome_ref is not None and self.outcome_ref.outcome_attempt is not None:
            if self.content or self.resource_refs or self.tool_execution is not None:
                raise ValueError("an Outcome reference cannot also own its observed content")
        if self.input_ref is not None:
            if self.input_ref.part == "body" and (self.content or self.semantic_attributes):
                raise ValueError("an Input body reference cannot also own message content")
            if self.input_ref.attachments_from_input and self.input_attachments:
                raise ValueError("referenced Input attachments cannot be copied into a message")
        return self

    def _owned_content(self) -> MessageRecord:
        """Borrow typed content for synchronous encoding without copying its bodies."""
        updates: dict[str, Any] = {}
        if self.input_ref is not None:
            if self.input_ref.part == "body":
                updates["content"] = []
                updates["semantic_attributes"] = {}
            if self.input_ref.attachments_from_input:
                updates["input_attachments"] = ()
        if self.call_refs:
            updates["tool_calls"] = []
        if self.outcome_ref is not None:
            updates["tool_result"] = None
            if self.outcome_ref.outcome_attempt is not None:
                updates.update(content=[], resource_refs=[], tool_execution=None)
        if "content" not in updates and any(
            isinstance(block, MediaContent) and block.resource_id is not None
            for block in self.content
        ):
            updates["content"] = [
                media_reference(block) if isinstance(block, MediaContent) else block
                for block in self.content
            ]
        return self.model_copy(update=updates) if updates else self

    def as_fact(self) -> MessageFact:
        """Return a detached fact when ownership crosses an in-memory boundary."""
        return MessageFact.model_validate(self._owned_content().model_dump())

    def storage_json(self) -> str:
        """Encode owned fields; declared empty defaults need no stored copy.

        Keep the schema, identity and timestamps explicit. Arbitrary metadata
        and provider payload values retain their own nulls and empty values.
        """
        record = self._owned_content()
        record.validate_tool_fields()
        record.validate_persisted_content()
        return record.model_dump_json(
            exclude_none=True,
            exclude={
                name
                for name in (
                    "content",
                    "resource_refs",
                    "input_attachments",
                    "call_refs",
                    "tool_calls",
                    "reasoning_content",
                    "metadata",
                )
                if not getattr(record, name)
            },
        )


class MessageFact(MessageRecord):
    """The persisted shape; Input, Call and Outcome units are never embedded."""

    @model_validator(mode="after")
    def validate_ownership(self) -> Self:
        return self.validate_persisted_content()


def model_visible_message(
    message: MessageRecord | ModelMessage, *, supports_inline_system_messages: bool = False
) -> ModelMessage:
    """Project one canonical message into the exact semantics sent to a model."""

    model_message = (
        ModelMessage.from_record(message) if isinstance(message, MessageRecord) else message
    )
    role = model_message.role
    if role == "harness":
        role = "system" if supports_inline_system_messages else "user"
    tag = model_message.semantic_tag
    if tag is None and not any(
        isinstance(block, SkillActivationContent) for block in model_message.content
    ):
        return (
            model_message
            if role == model_message.role
            else model_message.model_copy(update={"role": role})
        )
    content = [
        TextContent(text=block.render()) if isinstance(block, SkillActivationContent) else block
        for block in model_message.content
    ]
    if tag is None:
        return model_message.model_copy(update={"role": role, "content": content})
    # Preserve unchanged blocks so the request manifest can reference the original body.
    for index, block in enumerate(content):
        if isinstance(block, TextContent):
            escaped = escape_semantic_envelope_delimiters(block.text, tag)
            if escaped != block.text:
                content[index] = TextContent(text=escaped)
    return model_message.model_copy(
        update={
            "role": role,
            "content": [
                TextContent(text=f"{model_message.opening_tag()}\n"),
                *content,
                TextContent(text=f"\n</{tag}>"),
            ],
            # The projected envelope carries the label and its attributes exactly once.
            "semantic_tag": None,
            "semantic_attributes": {},
        }
    )


def model_visible_messages(
    message: MessageRecord | ModelMessage, *, supports_inline_system_messages: bool
) -> tuple[ModelMessage, ...]:
    """Present harness text at the declared role and its media in native user content."""
    visible = model_visible_message(
        message, supports_inline_system_messages=supports_inline_system_messages
    )
    if message.role == "harness" and supports_inline_system_messages:
        media: list[ContentBlock] = [
            block for block in visible.content if isinstance(block, MediaContent)
        ]
        if media:
            attachment = ModelMessage(role="user", content=media)
            text = [block for block in visible.content if not isinstance(block, MediaContent)]
            if text:
                return attachment, visible.model_copy(update={"content": text})
            return (attachment,)
    return (visible,)


def _validate_skill_activation_blocks(role: MessageRole, content: list[ContentBlock]) -> None:
    activations = [block for block in content if isinstance(block, SkillActivationContent)]
    if activations and role != "user":
        raise ValueError("skill activation content is only valid on user messages")
    if len({item.name for item in activations}) != len(activations):
        raise ValueError("skill activations must have unique names")
