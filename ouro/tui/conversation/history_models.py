"""History layout, render request and cached line records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from rich.text import Text
from textual.strip import Strip

from ouro.application.contracts.agents import AgentTreeNode
from ouro.application.contracts.execution import RuntimeToolCallActivity
from ouro.tui.state import RunPresentation
from ouro.tui.theme import TuiTheme


class ToolActivityRenderer(Protocol):
    def __call__(
        self,
        tool: RuntimeToolCallActivity,
        *,
        frame: int,
        theme: TuiTheme,
        agent: AgentTreeNode | None = None,
    ) -> Text: ...


TranscriptEntryKind = Literal[
    "user", "scaffold", "assistant", "tool", "notice", "command", "compaction", "run"
]


@dataclass(frozen=True)
class CompactionPresentation:
    covered_message_count: int
    tokens_reduced: int | None
    duration_seconds: float | None


@dataclass(frozen=True)
class TranscriptEntry:
    """Immutable presentation input for one persisted transcript boundary."""

    kind: TranscriptEntryKind
    text: str = ""
    created_at: datetime | None = None
    reasoning: str = ""
    run_id: str | None = None
    reasoning_seconds: float | None = None
    tool: RuntimeToolCallActivity | None = None
    agent: AgentTreeNode | None = None
    run: RunPresentation | None = None
    compaction: CompactionPresentation | None = None
    error: bool = False
    scaffold: bool = False


_PieceTag = Literal["body", "user", "reasoning"]


@dataclass
class _Piece:
    """One immutable render unit.

    Reasoning pieces carry their entry index: toggling visibility re-renders
    exactly those pieces (a plain-text render bounded by each block) while
    every other piece's strips are reused verbatim.
    """

    strips: list[Strip]
    tag: _PieceTag = "body"
    entry_index: int = -1


@dataclass(frozen=True)
class _EntryRenderRequest:
    """Detached input for rendering one history entry outside the UI loop."""

    entry: TranscriptEntry
    previous_kind: TranscriptEntryKind | None
    entry_index: int
    theme: TuiTheme
    reasoning_expanded: bool
    message_highlighted: bool = False
    tool_highlighted: bool = False
    reasoning_highlighted: bool = False
    trace_collapsed: bool | None = None
    trace_highlighted: bool = False


@dataclass(frozen=True)
class _RenderedEntry:
    entry_index: int
    pieces: tuple[tuple[list[Strip], _PieceTag], ...]


@dataclass(slots=True)
class ConversationHistoryState:
    """Detached rendered history owned by one Session.

    A state is transferred, not copied, and consumed by the next load of that
    Session.  Live transcript widgets deliberately stay outside this boundary.
    """

    entries: list[TranscriptEntry]
    pieces: list[_Piece]
    piece_line_starts: list[int]
    piece_indexes_by_entry: dict[int, list[int]]
    user_lines: list[int]
    lines: list[Strip]
    widest_line_width: int
    render_width: int
    rendered_count: int
    reasoning_expanded: bool
    reasoning_expanded_by_entry: dict[int, bool]
    theme: TuiTheme
    collapse_traces: bool
    trace_collapsed_by_run: dict[str, bool]
