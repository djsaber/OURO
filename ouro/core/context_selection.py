"""Current context policy state, independent of the last prepared request."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.errors import PersistenceError
from ouro.core.messages import MessageFact, MessageRecord


class MessageBoundary(OuroModel):
    message_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)


class ContextSelection(OuroModel):
    compaction_id: str | None = None
    media_elided_through: MessageBoundary | None = None

    def elided_ids(self, messages: Sequence[MessageRecord]) -> frozenset[str]:
        boundary = self.media_elided_through
        return frozenset(
            item.message_id
            for item in messages
            if boundary is not None and 0 < item.sequence <= boundary.sequence
        )

    def retain_prefix(self, last: MessageRecord | None) -> ContextSelection:
        boundary = self.media_elided_through
        if boundary is None or (last is not None and boundary.sequence <= last.sequence):
            return self
        return self.model_copy(
            update={
                "media_elided_through": (
                    MessageBoundary(message_id=last.message_id, sequence=last.sequence)
                    if last is not None
                    else None
                )
            }
        )


def validate_media_selection(
    selection: ContextSelection, session_id: str, source: MessageFact | MessageRecord | None
) -> None:
    boundary = selection.media_elided_through
    if boundary is not None and (
        source is None
        or source.session_id != session_id
        or source.message_id != boundary.message_id
        or source.sequence != boundary.sequence
    ):
        raise PersistenceError("media selection boundary does not match canonical history")
