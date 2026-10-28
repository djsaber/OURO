"""Provider-neutral content shared by messages and execution results."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable
from typing import Annotated, Literal, Self
from urllib.parse import unquote_to_bytes

from pydantic import Field, model_validator

from ouro.core.base import OuroModel
from ouro.core.resources import validate_resource_id


class TextContent(OuroModel):
    type: Literal["text"] = "text"
    text: str = ""


class _MediaSource(OuroModel):
    # A materialized read may carry both. Persisted content keeps only the resource.
    uri: str = ""
    resource_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    uri_prefix: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if self.resource_id is not None:
            validate_resource_id(self.resource_id)
        elif not self.uri:
            raise ValueError("media requires a URI or an owned resource")
        return self


class ImageContent(_MediaSource):
    type: Literal["image"] = "image"
    media_type: str = ""


class AudioContent(_MediaSource):
    type: Literal["audio"] = "audio"
    media_type: str = Field(pattern=r"^audio/[^\s;]+$")


class VideoContent(_MediaSource):
    type: Literal["video"] = "video"
    media_type: str = Field(pattern=r"^video/[^\s;]+$")


MediaModality = Literal["image", "audio", "video"]
MediaContent = ImageContent | AudioContent | VideoContent
ContentPart = Annotated[TextContent | MediaContent, Field(discriminator="type")]
ToolContent = str | list[ContentPart]


def media_reference(content: MediaContent) -> MediaContent:
    """Discard the read projection while retaining the immutable content owner."""
    return content.model_copy(update={"uri": ""}) if content.resource_id is not None else content


def media_resource_ids(content: Iterable[object]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            block.resource_id
            for block in content
            if isinstance(block, MediaContent) and block.resource_id is not None
        )
    )


def media_data_uri(uri: str) -> tuple[str, bytes] | None:
    """Decode an actual data URI; remote and opaque/invalid URIs remain literal."""
    prefix, separator, encoded = uri.partition(",")
    if not separator or not prefix.lower().startswith("data:"):
        return None
    if prefix.lower().endswith(";base64"):
        try:
            return prefix, base64.b64decode(
                b"".join(unquote_to_bytes(encoded).split()), validate=True
            )
        except binascii.Error, ValueError:
            return None
    return prefix + ";base64", unquote_to_bytes(encoded)


def same_content_observation(left: ToolContent, right: ToolContent) -> bool:
    """Resource addressing does not change the tool's observed media value."""
    if isinstance(left, str) or isinstance(right, str):
        return left == right
    if len(left) != len(right):
        return False
    for original, actual in zip(left, right, strict=True):
        if isinstance(original, MediaContent) and isinstance(actual, MediaContent):
            if original.type != actual.type or original.media_type != actual.media_type:
                return False
            if not original.uri or not actual.uri:
                if original != actual:
                    return False
                continue
            if original.uri == actual.uri:
                continue
            decoded = media_data_uri(original.uri)
            if decoded is None or decoded != media_data_uri(actual.uri):
                return False
        elif original != actual:
            return False
    return True


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
