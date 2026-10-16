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


class AudioContent(OuroModel):
    type: Literal["audio"] = "audio"
    uri: str = Field(min_length=1)
    media_type: str = Field(pattern=r"^audio/[^\s;]+$")


class VideoContent(OuroModel):
    type: Literal["video"] = "video"
    uri: str = Field(min_length=1)
    media_type: str = Field(pattern=r"^video/[^\s;]+$")


MediaModality = Literal["image", "audio", "video"]
MediaContent = ImageContent | AudioContent | VideoContent
ContentPart = Annotated[TextContent | MediaContent, Field(discriminator="type")]
ToolContent = str | list[ContentPart]


# A local planning allowance; duration and the provider determine actual media tokens.
MEDIA_CONTENT_COST = 2048


def input_modalities(
    *, supports_image_input: bool, supports_audio_input: bool, supports_video_input: bool
) -> frozenset[MediaModality]:
    capabilities: tuple[tuple[MediaModality, bool], ...] = (
        ("image", supports_image_input),
        ("audio", supports_audio_input),
        ("video", supports_video_input),
    )
    return frozenset(kind for kind, supported in capabilities if supported)


def media_format_omission(content_type: MediaModality, media_type: str) -> str | None:
    formats = {
        "image": ("image/png", "image/jpeg", "image/gif", "image/webp"),
        "audio": ("audio/wav", "audio/mpeg"),
        "video": ("video/mp4",),
    }
    if media_type not in formats[content_type]:
        return (
            f"The {content_type} format {media_type!r} cannot be sent. "
            f"Supported formats: {', '.join(formats[content_type])}."
        )
    return None


def unsupported_content_text(content_type: str) -> str:
    """Explain unavailable content without exposing routing or protocol details."""
    ability = {"image": "view images", "audio": "hear audio", "video": "view videos"}.get(
        content_type, f"read {content_type} content"
    )
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
