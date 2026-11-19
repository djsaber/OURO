from __future__ import annotations

import re
from typing import Annotated, Self
from xml.sax.saxutils import quoteattr

from pydantic import AfterValidator, Field, model_validator

from ouro.core.base import OuroModel

_RESERVED_SEMANTIC_TAGS = frozenset(
    {
        "assistant",
        "developer",
        "system",
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


SemanticTag = Annotated[
    str,
    Field(min_length=1, max_length=64),
    AfterValidator(validate_semantic_tag),
]

SemanticAttributes = dict[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")], str]


class MessageSemantics(OuroModel):
    """Model-visible envelope fields, independent of the message body and provenance."""

    semantic_tag: SemanticTag | None = None
    semantic_attributes: SemanticAttributes = Field(
        default_factory=dict, exclude_if=lambda attributes: not attributes
    )

    @model_validator(mode="after")
    def require_tag_for_attributes(self) -> Self:
        if self.semantic_attributes and self.semantic_tag is None:
            raise ValueError("semantic attributes require a semantic tag")
        return self

    def opening_tag(self) -> str:
        assert self.semantic_tag is not None
        fields = "".join(
            f" {key}={quoteattr(value)}" for key, value in self.semantic_attributes.items()
        )
        return f"<{self.semantic_tag}{fields}>"


def render_semantic_envelope(
    text: str, semantic_tag: str, attributes: SemanticAttributes | None = None
) -> str:
    """Project raw provider-owned text through one validated semantic envelope."""
    semantics = MessageSemantics(semantic_tag=semantic_tag, semantic_attributes=attributes or {})
    escaped = _escape_semantic_envelope_delimiters(text, semantic_tag)
    return f"{semantics.opening_tag()}\n{escaped}\n</{semantic_tag}>"


__all__ = [
    "MessageSemantics",
    "SemanticAttributes",
    "SemanticTag",
    "escape_semantic_envelope_delimiters",
    "render_semantic_envelope",
    "validate_semantic_tag",
]
