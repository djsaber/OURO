"""Fixed request message skeletons and references to immutable message content."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from ouro.core.base import OuroModel
from ouro.core.content import MediaContent, media_reference, media_resource_ids
from ouro.core.errors import PersistenceError
from ouro.core.message_roles import MessageRole
from ouro.core.message_semantics import SemanticTag
from ouro.core.messages import (
    ContentBlock,
    MessageRecord,
    ModelMessage,
    ProviderContinuationArtifact,
    ToolResultReference,
)
from ouro.core.tools import ToolCallRequest


class RequestMessageReference(OuroModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: str = Field(min_length=1)


class RequestContentReference(RequestMessageReference):
    type: Literal["message_content"] = "message_content"
    block_index: int = Field(ge=0)


RequestContent = Annotated[ContentBlock | RequestContentReference, Field(discriminator="type")]


class RequestMessage(OuroModel):
    """Final fields; references never ask the reader to run a projection."""

    model_config = ConfigDict(extra="forbid")

    role: MessageRole
    content: list[RequestContent] = Field(default_factory=list)
    semantic_tag: SemanticTag | None = None
    tool_calls: list[ToolCallRequest] | RequestMessageReference = Field(default_factory=list)
    tool_result: ToolResultReference | None = None
    provider_continuation: ProviderContinuationArtifact | RequestMessageReference | None = None

    @model_validator(mode="after")
    def validate_media_ownership(self) -> Self:
        if any(
            isinstance(block, MediaContent) and block.resource_id is not None and block.uri
            for block in self.content
        ):
            raise ValueError("media resource bytes cannot also be owned by a request")
        return self

    @classmethod
    def prepared(cls, value: ModelMessage, source: MessageRecord | None = None) -> RequestMessage:
        # Only unchanged objects from this preparation's canonical source may be
        # referenced. Transformations own their actual final values; no hash,
        # value matching, second projection or retained history cache is needed.
        blocks = (
            {id(block): index for index, block in enumerate(source.content)}
            if source is not None
            else {}
        )
        calls: list[ToolCallRequest] | RequestMessageReference = value.tool_calls
        continuation: ProviderContinuationArtifact | RequestMessageReference | None = (
            value.provider_continuation
        )
        if source is not None:
            if (
                value.tool_calls
                and len(value.tool_calls) == len(source.tool_calls)
                and all(
                    actual is original
                    for actual, original in zip(value.tool_calls, source.tool_calls, strict=True)
                )
            ):
                calls = RequestMessageReference(message_id=source.message_id)
            if continuation is not None and continuation is source.provider_continuation:
                continuation = RequestMessageReference(message_id=source.message_id)
        return cls(
            role=value.role,
            content=[
                RequestContentReference(message_id=source.message_id, block_index=blocks[id(block)])
                if source is not None and id(block) in blocks
                else media_reference(block)
                if isinstance(block, MediaContent)
                else block
                for block in value.content
            ],
            semantic_tag=value.semantic_tag,
            tool_calls=calls,
            tool_result=value.tool_result,
            provider_continuation=continuation,
        )

    def materialize(self, sources: Mapping[str, MessageRecord]) -> ModelMessage:
        content: list[ContentBlock] = []
        for block in self.content:
            if isinstance(block, RequestContentReference):
                try:
                    content.append(sources[block.message_id].content[block.block_index])
                except (KeyError, IndexError) as exc:
                    raise PersistenceError("request references missing message content") from exc
            else:
                content.append(block)
        calls = self.tool_calls
        continuation = self.provider_continuation
        try:
            if isinstance(calls, RequestMessageReference):
                calls = sources[calls.message_id].tool_calls
                if not calls:
                    raise PersistenceError("request references missing tool calls")
            if isinstance(continuation, RequestMessageReference):
                continuation = sources[continuation.message_id].provider_continuation
                if continuation is None:
                    raise PersistenceError("request references missing provider continuation")
        except KeyError as exc:
            raise PersistenceError("request references a missing message") from exc
        return ModelMessage(
            role=self.role,
            content=content,
            semantic_tag=self.semantic_tag,
            tool_calls=calls,
            tool_result=self.tool_result,
            provider_continuation=continuation,
        )


class RequestManifest(OuroModel):
    model_config = ConfigDict(extra="forbid")

    system_prompt: str = ""
    messages: list[RequestMessage] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def referenced_resource_ids(self) -> tuple[str, ...]:
        return media_resource_ids(block for message in self.messages for block in message.content)

    @property
    def referenced_message_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                item.message_id
                for message in self.messages
                for item in (*message.content, message.tool_calls, message.provider_continuation)
                if isinstance(item, RequestMessageReference)
            )
        )

    def materialize(
        self, session_id: str, sources: Mapping[str, MessageRecord]
    ) -> list[ModelMessage]:
        if any(
            message.session_id != session_id or message.message_id != identity
            for identity, message in sources.items()
        ):
            raise PersistenceError("request content belongs to another message or Session")
        return [message.materialize(sources) for message in self.messages]
