"""Bound result content without cutting through encoded media."""

from __future__ import annotations

from ouro.core.content import ContentPart, ImageContent, TextContent, ToolContent


def limit_result_content(
    content: ToolContent,
    *,
    text_limit: int | None,
    media_limit: int,
) -> tuple[ToolContent, bool]:
    if isinstance(content, str):
        if text_limit is None or len(content) <= text_limit:
            return content, False
        marker = f"\n...[tool output truncated at {text_limit} characters]"
        return (content[: max(0, text_limit - len(marker))] + marker)[:text_limit], True

    parts: list[ContentPart] = []
    media_left = media_limit
    truncated = False
    for part in content:
        if isinstance(part, ImageContent):
            # URLs and data URLs are retained intact. Counting their serialized
            # characters avoids decoding/copying image bytes on the execution path.
            if len(part.uri) > media_left:
                parts.append(TextContent(text="[media output limit exceeded; content omitted]"))
                truncated = True
                continue
            media_left -= len(part.uri)
        parts.append(part)

    text_size = sum(len(part.text) for part in parts if isinstance(part, TextContent))
    if text_limit is None or text_size <= text_limit:
        return parts, truncated
    marker = f"\n...[tool output truncated at {text_limit} characters]"[:text_limit]
    remaining = max(0, text_limit - len(marker))
    bounded: list[ContentPart] = []
    marked = False
    for part in parts:
        if not isinstance(part, TextContent):
            bounded.append(part)
            continue
        text = part.text[:remaining]
        remaining -= len(text)
        if remaining == 0 and not marked:
            text += marker
            marked = True
        if text:
            bounded.append(TextContent(text=text))
    return bounded, True
