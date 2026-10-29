"""An observer's applied fact and current-output positions."""

from __future__ import annotations

from ouro.core.base import OuroModel
from ouro.core.response_drafts import ResponseStreamPosition


class FactCursor(OuroModel):
    revision: int
    kind: int
    identity: str


class SessionObservationPosition(OuroModel):
    revision: int = 0
    message_sequence: int = 0
    facts: FactCursor | None = None
    draft: ResponseStreamPosition | None = None
