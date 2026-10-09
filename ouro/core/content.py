"""Provider-neutral content shared by messages and execution results."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from ouro.core.base import OuroModel


class TextContent(OuroModel):
    type: Literal["text"] = "text"
    text: str = ""


class ImageContent(OuroModel):
    type: Literal["image"] = "image"
    uri: str = Field(min_length=1)
    media_type: str = ""


ContentPart = Annotated[TextContent | ImageContent, Field(discriminator="type")]
ToolContent = str | list[ContentPart]


def unsupported_content_text(content_type: str) -> str:
    """Explain unavailable content without exposing routing or protocol details."""
    ability = "view images" if content_type == "image" else f"read {content_type} content"
    return (
        f"This model cannot {ability}. The {content_type} was not sent. "
        "Trying again or changing its size will not help."
    )


def content_parts(content: ToolContent) -> list[ContentPart]:
    """A string is the concise form of a single text part."""
    return [TextContent(text=content)] if isinstance(content, str) else list(content)


def content_text(content: ToolContent) -> str:
    """Extract text for human-readable surfaces without serializing media payloads."""
    if isinstance(content, str):
        return content
    return "".join(part.text for part in content if isinstance(part, TextContent))


def append_content_text(content: ToolContent, text: str) -> ToolContent:
    if not text:
        return content
    if isinstance(content, str):
        return f"{content}\n{text}" if content else text
    return [*content, TextContent(text=f"\n{text}" if content else text)]
