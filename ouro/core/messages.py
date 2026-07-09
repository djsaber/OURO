from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ouro.core.base import SCHEMA_VERSION, Metadata, MetadataField, OuroModel, new_id, utc_now


class TextContent(OuroModel):
    type: Literal["text"] = "text"
    text: str = ""


class ImageContent(OuroModel):
    type: Literal["image"] = "image"
    uri: str
    media_type: str = ""


ContentBlock = Annotated[TextContent | ImageContent, Field(discriminator="type")]

MessageRole = Literal["user", "assistant", "tool_result", "system_synthetic"]


class MessageRecord(OuroModel):
    schema_version: int = SCHEMA_VERSION
    message_id: str = Field(default_factory=lambda: new_id("msg"))
    session_id: str
    run_id: str | None = None
    turn_id: str | None = None
    role: MessageRole
    created_at: object = Field(default_factory=utc_now)
    sequence: int = 0
    content: list[ContentBlock] = Field(default_factory=list)
    synthetic: bool = False
    resource_refs: list[str] = Field(default_factory=list)
    metadata: Metadata = MetadataField

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
    ) -> MessageRecord:
        return cls(
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            role=role,
            sequence=sequence,
            synthetic=role == "system_synthetic",
            content=[TextContent(text=text)],
        )

    @property
    def text_content(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, TextContent))
