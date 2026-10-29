"""Positions and bounded current-fact pages used by Session observation."""

from __future__ import annotations

from ouro.core.base import OuroModel
from ouro.core.compaction import CompactionRecord
from ouro.core.inputs import InputState
from ouro.core.session_observation import FactCursor
from ouro.core.state import RunRecord
from ouro.core.tool_batches import ToolCallState


class SessionFactPage(OuroModel):
    revision: int
    runs: tuple[RunRecord, ...] = ()
    inputs: tuple[InputState, ...] = ()
    calls: tuple[ToolCallState, ...] = ()
    compactions: tuple[CompactionRecord, ...] = ()
    next_cursor: FactCursor | None = None
