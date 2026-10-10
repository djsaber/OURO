"""Content placement policy at the provider boundary, independent of tool names."""

from __future__ import annotations

from dataclasses import dataclass

from ouro.core.content import (
    ContentPart,
    MediaContent,
    TextContent,
    media_format_omission,
    unsupported_content_text,
)
from ouro.core.messages import ModelMessage
from ouro.core.tool_result_projection import ToolResultPresentation


@dataclass(frozen=True)
class ToolContentPolicy:
    """Types accepted natively, or as supplemental user context, by this route."""

    native: frozenset[str]
    contextual: frozenset[str] = frozenset()

    def __call__(self, message: ModelMessage) -> ToolResultPresentation:
        content: list[ContentPart] = []
        context: list[ContentPart] = []
        for part in message.content:
            if not isinstance(part, TextContent | MediaContent):
                raise ValueError(f"unsupported tool content type: {part.type}")
            if isinstance(part, MediaContent) and part.type in self.native | self.contextual:
                reason = (
                    media_format_omission(part.type, part.media_type) if part.media_type else None
                )
                if reason:
                    content.append(TextContent(text=f"\n{reason}\n"))
                    continue
            if part.type in self.native:
                content.append(part)
            elif part.type in self.contextual:
                content.append(TextContent(text=f"\nThe {part.type} is included below.\n"))
                context.append(part)
            else:
                content.append(TextContent(text=f"\n{unsupported_content_text(part.type)}\n"))
        return ToolResultPresentation(
            content=tuple(content),
            context=(tuple(context),) if context else (),
        )
