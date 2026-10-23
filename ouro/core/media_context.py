"""Monotonic media elision within one canonical history epoch."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.content import MediaContent, MediaModality
from ouro.core.errors import MediaCapacityError
from ouro.core.message_frames import context_frames, project_tool_exchanges
from ouro.core.messages import MessageRecord

MEDIA_RETENTION_RATIO = 0.6
MEDIA_CONTEXT_KEY = "media_projection"


class MediaElisionState(OuroModel):
    history_epoch: int = Field(ge=0)
    media_elided_through_message_id: str | None = None

    def elided_ids(self, messages: Sequence[MessageRecord]) -> frozenset[str]:
        for index, message in enumerate(messages):
            if message.message_id == self.media_elided_through_message_id:
                return frozenset(item.message_id for item in messages[: index + 1])
        return frozenset()


def media_chars(content: MediaContent) -> int:
    separator = content.uri.find(",") if content.uri.startswith("data:") else -1
    return len(content.uri) - separator - 1 if separator >= 0 else len(content.uri)


def historical_media_text(kind: MediaModality) -> str:
    return (
        f"[Historical {kind}]\n"
        f"The {kind} data is not included here. Original file information and conversation "
        "text remain, but may not describe every detail. Use existing observations first; "
        "read an available source again only when unrecorded details need verification. "
        "The original file may have changed.\n"
        f"[/Historical {kind}]"
    )


def require_media_capacity(size: int, limit: int) -> None:
    if size > limit:
        raise MediaCapacityError(
            f"Pending media requires {size} encoded characters, exceeding the request media "
            f"limit of {limit}. Use smaller media or split the input into separate batches. "
            "No model request was sent."
        )


def plan_media_elision(
    messages: Sequence[MessageRecord],
    sizes: Mapping[str, int],
    state: MediaElisionState,
    *,
    limit: int,
) -> MediaElisionState:
    """Release whole old frames only on overflow, protecting all unprocessed input.

    A completed model reply closes its preceding input, not its own tool results.
    Incomplete exchanges protect their entire suffix even if a later reply exists.
    Sizes describe only the uncovered, sendable request; positions remain canonical.
    """
    elided = state.elided_ids(messages)
    remaining = sum(size for identity, size in sizes.items() if identity not in elided)
    if remaining <= limit:
        return state

    protected = 0
    for index, message in enumerate(messages):
        if (
            message.role == "assistant"
            and message.model_response is not None
            and not message.metadata.get("incomplete")
        ):
            protected = index + 1
    _, excluded = project_tool_exchanges(messages)
    excluded_ids = set(excluded)
    for index, message in enumerate(messages):
        if message.message_id in excluded_ids:
            protected = min(protected, index)
            break

    eligible: list[tuple[str, int]] = []
    end = 0
    for frame in context_frames(messages):
        end += len(frame)
        if end > protected:
            break
        size = sum(sizes.get(m.message_id, 0) for m in frame if m.message_id not in elided)
        if size:
            eligible.append((frame[-1].message_id, size))
    pending = remaining - sum(size for _, size in eligible)
    require_media_capacity(pending, limit)
    target = max(int(limit * MEDIA_RETENTION_RATIO), pending)
    boundary = state.media_elided_through_message_id
    for identity, size in eligible:
        remaining -= size
        boundary = identity
        if remaining <= target:
            break
    return state.model_copy(update={"media_elided_through_message_id": boundary})
