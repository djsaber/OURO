"""Translate tool content across the standalone SDK and worker boundaries."""

from __future__ import annotations

from pydantic import TypeAdapter

from ouro.core.content import (
    AudioContent,
    ContentPart,
    ImageContent,
    TextContent,
    ToolContent,
    VideoContent,
)
from ouro.sdk.plugins import v3 as sdk

_CONTENT: TypeAdapter[ToolContent] = TypeAdapter(ToolContent)


def from_sdk_content(content: sdk.ToolContent) -> ToolContent:
    if isinstance(content, str):
        return content
    parts: list[ContentPart] = []
    for part in content:
        if isinstance(part, sdk.ToolTextContent):
            parts.append(TextContent(text=part.text))
        elif isinstance(part, sdk.ToolImageContent):
            parts.append(ImageContent(uri=part.uri, media_type=part.media_type))
        elif isinstance(part, sdk.ToolAudioContent):
            parts.append(AudioContent(uri=part.uri, media_type=part.media_type))
        elif isinstance(part, sdk.ToolVideoContent):
            parts.append(VideoContent(uri=part.uri, media_type=part.media_type))
        else:
            raise ValueError("tool output content must contain typed SDK content parts")
    return parts


def decode_worker_content(value: object) -> sdk.ToolContent:
    content = _CONTENT.validate_python(value, strict=True)
    if isinstance(content, str):
        return content
    return tuple(
        sdk.ToolTextContent(text=part.text)
        if isinstance(part, TextContent)
        else {
            "image": sdk.ToolImageContent,
            "audio": sdk.ToolAudioContent,
            "video": sdk.ToolVideoContent,
        }[part.type](uri=part.uri, media_type=part.media_type)
        for part in content
    )
