from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, Field

DEFAULT_SYNTHETIC_MESSAGE_TAG = "system-reminder"

_RESERVED_SEMANTIC_TAGS = frozenset(
    {
        "assistant",
        "developer",
        "system",
        "system-synthetic",
        "tool",
        "tool-result",
        "user",
    }
)


def validate_semantic_tag(value: str) -> str:
    if not 1 <= len(value) <= 64:
        raise ValueError("semantic tag length must be between 1 and 64 characters")
    if re.fullmatch(r"[a-z][a-z0-9-]*", value) is None:
        raise ValueError(
            "semantic tag must start with a lowercase letter and contain only "
            "lowercase letters, digits, or hyphens"
        )
    if value in _RESERVED_SEMANTIC_TAGS:
        raise ValueError(f"semantic tag {value!r} is reserved for a model role")
    return value


def escape_semantic_envelope_delimiters(text: str, semantic_tag: str) -> str:
    """Escape only delimiters that could alter the declared semantic envelope."""

    tag = validate_semantic_tag(semantic_tag)
    return _escape_semantic_envelope_delimiters(text, tag)


def _escape_semantic_envelope_delimiters(text: str, tag: str) -> str:
    pattern = re.compile(
        rf"<\s*/?\s*{re.escape(tag)}(?:\s+[^<>]*?)?\s*/?\s*>",
        re.IGNORECASE,
    )
    return pattern.sub(
        lambda match: match.group(0).replace("<", "&lt;").replace(">", "&gt;"),
        text,
    )


def render_semantic_envelope(text: str, semantic_tag: str) -> str:
    """Project raw provider-owned text through one validated semantic envelope."""

    tag = validate_semantic_tag(semantic_tag)
    escaped = _escape_semantic_envelope_delimiters(text, tag)
    return f"<{tag}>\n{escaped}\n</{tag}>"


SemanticTag = Annotated[
    str,
    Field(min_length=1, max_length=64),
    AfterValidator(validate_semantic_tag),
]


__all__ = [
    "DEFAULT_SYNTHETIC_MESSAGE_TAG",
    "SemanticTag",
    "escape_semantic_envelope_delimiters",
    "render_semantic_envelope",
    "validate_semantic_tag",
]
