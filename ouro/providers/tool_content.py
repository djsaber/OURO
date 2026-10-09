"""Content placement policy at the provider boundary, independent of tool names."""

from __future__ import annotations

from dataclasses import dataclass

from ouro.core.content import ContentPart, ImageContent, TextContent
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
            if not isinstance(part, TextContent | ImageContent):
                raise ValueError(f"unsupported tool content type: {part.type}")
            if part.type in self.native:
                content.append(part)
            elif part.type in self.contextual:
                content.append(
                    TextContent(text=f"[{part.type} content supplied in following tool context]")
                )
                context.append(part)
            else:
                content.append(
                    TextContent(
                        text=(
                            f"[{part.type} content not supplied: "
                            "target endpoint does not support it]"
                        )
                    )
                )
        return ToolResultPresentation(
            content=tuple(content),
            context=(tuple(context),) if context else (),
        )
