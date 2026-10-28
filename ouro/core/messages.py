from __future__ import annotations

from datetime import datetime
from html import escape as escape_html
from typing import Annotated, Any, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from ouro.core.base import Metadata, MetadataField, OuroModel, VersionedOuroModel, new_id, utc_now
from ouro.core.content import ImageContent as ImageContent
from ouro.core.content import MediaContent, media_reference, media_resource_ids
from ouro.core.content import TextContent as TextContent
from ouro.core.input_content import InputContent, InputFile
from ouro.core.message_roles import MessageRole
from ouro.core.message_semantics import (
    SemanticTag,
    escape_semantic_envelope_delimiters,
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
    """Native replay data owned by its source route, alongside canonical history.

    Core persists opaque blocks; the target codec decides replay compatibility.
    Incompatible routes continue from canonical content and complete tool frames.
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


class ModelMessage(OuroModel):
    """Provider-neutral message containing only model-visible semantics.

    Canonical ``MessageRecord`` identity, persistence metadata and resource
    references deliberately stop at the context projection boundary.
    """

    role: MessageRole
    content: list[ContentBlock] = Field(default_factory=list)
    semantic_tag: SemanticTag | None = None
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    tool_result: ToolResultReference | None = None
    provider_continuation: ProviderContinuationArtifact | None = None

    @model_validator(mode="after")
    def validate_tool_fields(self) -> ModelMessage:
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant model messages may contain tool_calls")
        if self.tool_result is not None and self.role != "tool_result":
            raise ValueError("only tool_result model messages may contain tool_result")
        if self.provider_continuation is not None and self.role != "assistant":
            raise ValueError("only assistant model messages may contain provider continuation")
        _validate_skill_activation_blocks(self.role, self.content)
        return self

    @classmethod
    def from_record(cls, message: MessageRecord) -> ModelMessage:
        return cls(
            role=message.role,
            content=list(message.content),
            semantic_tag=message.semantic_tag,
            tool_calls=list(message.tool_calls),
            tool_result=message.tool_result,
            provider_continuation=message.provider_continuation,
        )

    @classmethod
    def text(
        cls,
        *,
        role: MessageRole,
        text: str,
        semantic_tag: SemanticTag | None = None,
    ) -> ModelMessage:
        return cls(
            role=role,
            content=[TextContent(text=text)],
            semantic_tag=semantic_tag,
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


class MessageRecord(VersionedOuroModel):
    """One explicitly materialized message for execution or a selected history read.

    Persistence uses ``as_fact``; resolving an Input never changes its content owner.
    This value has no loader, lazy collection, or implicit storage access.
    """

    schema_version: Literal[8] = 8
    message_id: str = Field(default_factory=lambda: new_id("msg"))
    before_harness_version: str | None = None
    session_id: str
    run_id: str | None = None
    turn_id: str | None = None
    role: MessageRole
    created_at: datetime = Field(default_factory=utc_now)
    sequence: int = 0
    content: list[ContentBlock] = Field(default_factory=list)
    scaffold: bool = False
    semantic_tag: SemanticTag | None = None
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
            "scaffold",
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
            if self.role != "user":
                raise ValueError("input attachments require a user-role message")
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
        scaffold: bool = False,
        semantic_tag: SemanticTag | None = None,
    ) -> MessageRecord:
        return cls(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            role=role,
            sequence=sequence,
            scaffold=scaffold,
            semantic_tag=semantic_tag,
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
            if self.input_ref.part == "body" and self.content:
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


def model_visible_message(message: MessageRecord | ModelMessage) -> ModelMessage:
    """Project one canonical message into the exact semantics sent to a model."""

    model_message = (
        ModelMessage.from_record(message) if isinstance(message, MessageRecord) else message
    )
    projected_content = [_model_visible_content(block) for block in model_message.content]
    model_message = model_message.model_copy(update={"content": projected_content})
    tag = model_message.semantic_tag
    if tag is None:
        return model_message
    escaped_content = [
        block.model_copy(update={"text": escape_semantic_envelope_delimiters(block.text, tag)})
        if isinstance(block, TextContent)
        else block
        for block in model_message.content
    ]
    return model_message.model_copy(
        update={
            "content": [
                TextContent(text=f"<{tag}>\n"),
                *escaped_content,
                TextContent(text=f"\n</{tag}>"),
            ],
            # The envelope now carries the model-visible label. Clearing the
            # typed field makes this projection idempotent across boundaries.
            "semantic_tag": None,
        }
    )


def _validate_skill_activation_blocks(role: MessageRole, content: list[ContentBlock]) -> None:
    activations = [block for block in content if isinstance(block, SkillActivationContent)]
    if activations and role != "user":
        raise ValueError("skill activation content is only valid on user messages")
    if len({item.name for item in activations}) != len(activations):
        raise ValueError("skill activations must have unique names")


def _model_visible_content(block: ContentBlock) -> TextContent | MediaContent:
    if not isinstance(block, SkillActivationContent):
        return block
    tag = "ouro-skill-activation"
    instructions = escape_semantic_envelope_delimiters(block.instructions, tag)
    attributes = {
        "name": block.name,
        "source": block.source,
        "location": block.location,
        "base-directory": block.base_directory,
        "digest": block.content_digest,
    }
    rendered_attributes = "".join(
        f' {name}="{escape_html(value, quote=True)}"' for name, value in attributes.items()
    )
    return TextContent(
        text=(
            "The user explicitly activated this skill. Follow its instructions for this "
            "request.\n\n"
            f"<{tag}{rendered_attributes}>\n"
            f"{instructions}\n"
            f"</{tag}>\n\n"
        )
    )
