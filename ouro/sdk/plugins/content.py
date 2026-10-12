from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ToolTextContent:
    text: str
    type: Literal["text"] = "text"


@dataclass(frozen=True)
class ToolImageContent:
    uri: str
    media_type: str = ""
    type: Literal["image"] = "image"


@dataclass(frozen=True)
class ToolAudioContent:
    uri: str
    media_type: str
    type: Literal["audio"] = "audio"


@dataclass(frozen=True)
class ToolVideoContent:
    uri: str
    media_type: str
    type: Literal["video"] = "video"


ToolContent = (
    str | tuple[ToolTextContent | ToolImageContent | ToolAudioContent | ToolVideoContent, ...]
)
