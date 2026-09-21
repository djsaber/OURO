"""Immutable personal instructions resolved at a request preparation boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ouro.core.base import Metadata


@dataclass(frozen=True)
class PromptContext:
    system_prompt: str
    source: Literal["static", "default", "replacement", "override"] = "static"
    revision: int | None = None
    replacement_id: str | None = None
    addition_ids: tuple[str, ...] = ()

    def metadata(self) -> Metadata:
        return {
            "source": self.source,
            "revision": self.revision,
            "replacement_id": self.replacement_id,
            "addition_ids": list(self.addition_ids),
        }


# A resolver reads current configuration or raises ConfigError; it must not cache
# request state. Context preparation owns the lifetime of its returned snapshot.
PromptContextResolver = Callable[[], PromptContext]
