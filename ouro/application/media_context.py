"""Prepare media projections and recover their boundary from the latest request."""

from __future__ import annotations

from typing import Protocol

from pydantic import ValidationError

from ouro.application.file_inputs import PreparedFileInputs, prepare_inline_messages
from ouro.core.content import MediaModality
from ouro.core.errors import PersistenceError
from ouro.core.media_context import MEDIA_CONTEXT_KEY, MediaElisionState, plan_media_elision
from ouro.core.messages import MessageRecord
from ouro.core.state import SessionRecord
from ouro.sessions import SessionStore


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

    def __init__(self, store: SessionStore, prepare_messages: ModelMessagePreparer | None) -> None:
        self.store = store
        self.prepare_messages = prepare_messages

    def project(
        self,
        messages: list[MessageRecord],
        *,
        session: SessionRecord,
        history: list[MessageRecord],
        input_modalities: frozenset[MediaModality],
        input_budget_tokens: int,
    ) -> tuple[list[MessageRecord], MediaElisionState]:
        state = self._state(session, history)
        prepared = (
            self.prepare_messages(
                messages,
                input_modalities=input_modalities,
                input_budget_tokens=input_budget_tokens,
            )
            if self.prepare_messages is not None
            else prepare_inline_messages(messages, input_modalities=input_modalities)
        )
        state = plan_media_elision(history, prepared.media_sizes, state, limit=prepared.media_limit)
        return prepared.materialize(state.elided_ids(history)), state

    def _state(self, session: SessionRecord, history: list[MessageRecord]) -> MediaElisionState:
        header = self.store.load_latest_context_header(session.session_id)
        raw = header.context_metadata.get(MEDIA_CONTEXT_KEY) if header is not None else None
        fresh = MediaElisionState(history_epoch=session.history_epoch)
        if raw is None:
            return fresh
        try:
            state = MediaElisionState.model_validate(raw)
        except ValidationError as exc:
            raise PersistenceError("invalid media projection state in latest context") from exc
        if state.history_epoch != session.history_epoch:
            return fresh
        if state.media_elided_through_message_id is not None and not state.elided_ids(history):
            return fresh
        return state
