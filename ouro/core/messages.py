from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from ouro.core.base import SCHEMA_VERSION, Metadata, MetadataField, OuroModel, new_id, utc_now
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
    resource_refs: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    tool_result: ToolResultReference | None = None
    metadata: Metadata = MetadataField

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_tool_metadata(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        metadata = migrated.get("metadata")
        if not isinstance(metadata, dict):
            return migrated
        if not migrated.get("tool_calls") and isinstance(metadata.get("tool_calls"), list):
            migrated["tool_calls"] = [
                {"name": "", **call} if isinstance(call, dict) else call
                for call in metadata["tool_calls"]
            ]
        if migrated.get("tool_result") is None and isinstance(metadata.get("tool_call_id"), str):
            migrated["tool_result"] = {
                "tool_call_id": metadata["tool_call_id"],
                "tool_name": str(metadata.get("tool_name") or ""),
                "is_error": bool(metadata.get("is_error", False)),
            }
        return migrated

    @model_validator(mode="after")
    def validate_tool_fields(self) -> MessageRecord:
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages may contain tool_calls")
        if self.tool_result is not None and self.role != "tool_result":
            raise ValueError("only tool_result messages may contain tool_result")
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
