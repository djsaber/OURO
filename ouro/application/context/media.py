"""Apply the current media selection without reading or changing request history."""

from __future__ import annotations

from typing import Protocol

from ouro.application.inputs.files import PreparedFileInputs, prepare_inline_messages
from ouro.core.content import MediaModality
from ouro.core.context_selection import ContextSelection
from ouro.core.media_context import plan_media_elision
from ouro.core.messages import MessageRecord
from ouro.core.state import SessionRecord


class ModelMessagePreparer(Protocol):
    def __call__(
        self,
        messages: list[MessageRecord],
        *,
        input_modalities: frozenset[MediaModality],
        input_budget_tokens: int | None = None,
    ) -> PreparedFileInputs: ...


class MediaContextPreparation:
    """Produce candidate projections without committing state or generating summaries."""

    def __init__(self, prepare_messages: ModelMessagePreparer | None) -> None:
        self.prepare_messages = prepare_messages

    def prepare(
        self,
        messages: list[MessageRecord],
        *,
        session: SessionRecord,
        input_modalities: frozenset[MediaModality],
        input_budget_tokens: int,
    ) -> tuple[PreparedFileInputs, ContextSelection]:
        prepared = (
            self.prepare_messages(
                messages,
                input_modalities=input_modalities,
                input_budget_tokens=input_budget_tokens,
            )
            if self.prepare_messages is not None
            else prepare_inline_messages(messages, input_modalities=input_modalities)
        )
        state = plan_media_elision(
            messages, prepared.media_sizes, session.context_selection, limit=prepared.media_limit
        )
        return prepared, state
