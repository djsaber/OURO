from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from ouro.core.base import SCHEMA_VERSION, Metadata, MetadataField, OuroModel, new_id, utc_now
from ouro.core.message_semantics import DEFAULT_SYNTHETIC_MESSAGE_TAG, SemanticTag
from ouro.core.tools import ToolCallRequest


class TextContent(OuroModel):
    type: Literal["text"] = "text"
    text: str = ""


class ImageContent(OuroModel):
    type: Literal["image"] = "image"
    uri: str
    media_type: str = ""


ContentBlock = Annotated[TextContent | ImageContent, Field(discriminator="type")]

MessageRole = Literal["user", "assistant", "tool_result", "system_synthetic"]


class ToolResultReference(OuroModel):
    tool_call_id: str
    tool_name: str
    is_error: bool = False


class ProviderContinuationArtifact(OuroModel):
    """Route-scoped provider state that Core persists but never interprets."""

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
        if self.semantic_tag is not None and self.role != "system_synthetic":
            raise ValueError("only system_synthetic model messages may have a semantic tag")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant model messages may contain tool_calls")
        if self.tool_result is not None and self.role != "tool_result":
            raise ValueError("only tool_result model messages may contain tool_result")
        if self.provider_continuation is not None and self.role != "assistant":
            raise ValueError("only assistant model messages may contain provider continuation")
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
        if role == "system_synthetic" and semantic_tag is None:
            semantic_tag = DEFAULT_SYNTHETIC_MESSAGE_TAG
        return cls(
            role=role,
            content=[TextContent(text=text)],
            semantic_tag=semantic_tag,
        )

    @property
    def text_content(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, TextContent))


class MessageRecord(OuroModel):
    schema_version: int = SCHEMA_VERSION
    message_id: str = Field(default_factory=lambda: new_id("msg"))
    session_id: str
    run_id: str | None = None
    turn_id: str | None = None
    role: MessageRole
    created_at: datetime = Field(default_factory=utc_now)
    sequence: int = 0
    content: list[ContentBlock] = Field(default_factory=list)
    synthetic: bool = False
    semantic_tag: SemanticTag | None = None
    resource_refs: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    tool_result: ToolResultReference | None = None
    reasoning_content: str = ""
    provider_continuation: ProviderContinuationArtifact | None = None
    metadata: Metadata = MetadataField

    @model_validator(mode="after")
    def validate_tool_fields(self) -> MessageRecord:
        reserved_metadata = {
            "tool_calls",
            "tool_call_id",
            "tool_name",
            "is_error",
            "reasoning_content",
            "provider_continuation",
            "semantic_tag",
        }
        misplaced = sorted(reserved_metadata.intersection(self.metadata))
        if misplaced:
            fields = ", ".join(misplaced)
            raise ValueError(
                f"structured state must use typed message fields, not metadata: {fields}"
            )
        if self.turn_id is not None and self.run_id is None:
            raise ValueError("message turn_id requires run_id")
        if self.role == "system_synthetic":
            if self.semantic_tag is None:
                self.semantic_tag = DEFAULT_SYNTHETIC_MESSAGE_TAG
        elif self.semantic_tag is not None:
            raise ValueError("only system_synthetic messages may have a semantic tag")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages may contain tool_calls")
        if self.tool_result is not None and self.role != "tool_result":
            raise ValueError("only tool_result messages may contain tool_result")
        if (self.reasoning_content or self.provider_continuation is not None) and (
            self.role != "assistant"
        ):
            raise ValueError("only assistant messages may contain reasoning state")
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
    ) -> MessageRecord:
        return cls(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            role=role,
            sequence=sequence,
            synthetic=role == "system_synthetic",
            semantic_tag=semantic_tag,
            content=[TextContent(text=text)],
        )

    @property
    def text_content(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, TextContent))


def model_visible_message(message: MessageRecord | ModelMessage) -> ModelMessage:
    """Project one canonical message into the exact semantics sent to a model."""

    model_message = (
        ModelMessage.from_record(message) if isinstance(message, MessageRecord) else message
    )
    tag = model_message.semantic_tag
    if tag is None:
        return model_message
    escaped_content = [
        block.model_copy(update={"text": _escape_envelope_delimiters(block.text, tag)})
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


def _escape_envelope_delimiters(text: str, tag: str) -> str:
    pattern = re.compile(
        rf"<\s*/?\s*{re.escape(tag)}(?:\s+[^<>]*?)?\s*/?\s*>",
        re.IGNORECASE,
    )
    return pattern.sub(
        lambda match: match.group(0).replace("<", "&lt;").replace(">", "&gt;"),
        text,
    )
