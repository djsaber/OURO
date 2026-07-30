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


SemanticTag = Annotated[
    str,
    Field(min_length=1, max_length=64),
    AfterValidator(validate_semantic_tag),
]


__all__ = ["DEFAULT_SYNTHETIC_MESSAGE_TAG", "SemanticTag", "validate_semantic_tag"]
