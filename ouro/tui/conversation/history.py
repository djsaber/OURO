"""Conversation history widget and asynchronous render ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import replace

from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.cache import LRUCache
from textual.events import Resize
from textual.geometry import Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.selection import Selection
from textual.strip import Strip
from textual.worker import Worker

from ouro.application.contracts.agents import AgentTreeNode
from ouro.application.contracts.execution import RuntimeToolCallActivity
from ouro.tui.conversation.history_models import (
    ConversationHistoryState,
    HistoryLine,
    PreparedLine,
    ReasoningLayout,
    ToolActivityRenderer,
    TranscriptEntry,
    TranscriptEntryKind,
    _EntryRenderRequest,
    _Piece,
    _RenderedEntry,
)
from ouro.tui.conversation.history_rendering import (
    _apply_selection_style,
    _render_entry_requests,
)
from ouro.tui.conversation.markdown_stream import _BACKGROUND_MARKDOWN_CHARS
from ouro.tui.conversation.messages import (
    _MESSAGE_META,
    _REASONING_ENTRY_META,
    _TOOL_NAVIGATION_META,
    _TRACE_ENTRY_META,
    MessageCopyRequested,
    ToolNavigationRequested,
)
from ouro.tui.selection import RangeSelectionOnly
from ouro.tui.theme import TuiTheme, theme_for

_LOAD_CHUNK_ENTRIES = 50


def _shift_piece_indexes(pieces: list[_Piece], offset: int) -> None:
    """Retain rendered text while moving its click targets to new entry indexes."""
    for piece in pieces:
        piece.entry_index += offset
        shifted: list[HistoryLine] = []
        for strip in piece.rows:
            if isinstance(strip, PreparedLine):
                shifted.append(strip)
                continue
            segments: list[Segment] = []
            changed = False
            for segment in strip:
                style = segment.style
                if style is not None:
                    metadata = {
                        key: value + offset
                        for key, value in style.meta.items()
                        if key
                        in {
                            _REASONING_ENTRY_META,
                            _TRACE_ENTRY_META,
                            _TOOL_NAVIGATION_META,
                            _MESSAGE_META,
                        }
                        and isinstance(value, int)
                    }
                    if metadata:
                        segment = Segment(
                            segment.text, style + Style(meta=metadata), segment.control
                        )
                        changed = True
                segments.append(segment)
            shifted.append(Strip(segments, strip.cell_length) if changed else strip)
        piece.rows = shifted


class ConversationHistory(RangeSelectionOnly, ScrollView, can_focus=False):
    """Line-buffered rendering for immutable persisted conversation history.

    Logical entries own visibility before rendering. Only visible entries need
    row geometry; expanding a trace fills its missing rows through the same
    render owner used by loading, streaming and reflow. Compatible rows survive
    visibility changes. Mutable output remains in the live widgets below.
    """

    DEFAULT_CSS = """
    ConversationHistory {
        background: $surface;
        color: $foreground;
        overflow-y: scroll;
    }
    """

    class ToggleReasoningRequested(Message):
        def __init__(self, entry_index: int) -> None:
            super().__init__()
            self.entry_index = entry_index

    class ToggleTraceRequested(Message):
        def __init__(self, entry_index: int) -> None:
            super().__init__()
            self.entry_index = entry_index

    class PrefixRendered(Message):
        def __init__(self, line_delta: int, revision: int) -> None:
            super().__init__()
            self.line_delta = line_delta
            self.revision = revision

    def __init__(
        self,
        render_tool: ToolActivityRenderer,
        *,
        reasoning_expanded: bool = True,
        collapse_traces: bool = True,
    ) -> None:
        super().__init__(id="conversation-history")
        self.lines: list[HistoryLine] = []
        self._line_cache: LRUCache[tuple[int, int, int], Strip] = LRUCache(1024)
        self._widest_line_width = 0
        self._size_known = False
        self.display = False
        self._render_tool = render_tool
        self._entries: list[TranscriptEntry] = []
        self._pieces: list[_Piece] = []
        self._piece_line_starts: list[int] = []
        self._piece_indexes_by_entry: dict[int, list[int]] = {}
        self._tool_entry_indexes: dict[tuple[str | None, str | None, str], int] = {}
        self._user_lines: list[int] = []
        self._render_width = 0
        self._reasoning_expanded = reasoning_expanded
        self._reasoning_expanded_by_entry: dict[int, bool] = {}
        self._collapse_traces = collapse_traces
        self._trace_collapsed_by_run: dict[str, bool] = {}
        self._trace_run_by_entry: dict[int, str] = {}
        self._trace_summary_indexes: dict[str, int] = {}
        self._open_trace_entries: dict[str, list[int]] = {}
        self._visibility_pending = False
        self._prepared_reasoning: dict[int, ReasoningLayout] = {}
        self._pending_history_state: ConversationHistoryState | None = None
        self._trace_visibility_dirty = False
        self._hovered_trace_entry_index: int | None = None
        self._hovered_message_entry_index: int | None = None
        self._hovered_tool_entry_index: int | None = None
        self._hovered_reasoning_entry_index: int | None = None
        self._rendered_count = 0
        self._prefix_pending = 0
        self._render_failed = False
        self._render_theme: TuiTheme | None = None
        self._content_revision = 0
        self._full_render_pending = False
        self._render_revision = 0
        self._render_active = False
        self._render_idle = asyncio.Event()
        self._render_idle.set()

    @property
    def entries(self) -> tuple[TranscriptEntry, ...]:
        return tuple(self._entries)

    @property
    def last_entry_kind(self) -> TranscriptEntryKind | None:
        return self._entries[-1].kind if self._entries else None

    @property
    def content_revision(self) -> int:
        return self._content_revision

    @property
    def render_complete(self) -> bool:
        return (
            self._rendered_count == len(self._entries)
            and not self._prefix_pending
            and not self._render_active
            and not self._visibility_pending
            and not self._full_render_pending
        )

    def on_conversation_history_prefix_rendered(self, message: PrefixRendered) -> None:
        if message.revision != self._content_revision:
            message.stop()

    @property
    def user_turns(self) -> tuple[str, ...]:
        return tuple(entry.text for entry in self._entries if entry.kind == "user")

    @property
    def user_lines(self) -> tuple[int, ...]:
        return tuple(self._user_lines)

    @property
    def rendered_text(self) -> str:
        return "\n".join(line.text.rstrip() for line in self.lines)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        return selection.extract(self.rendered_text), "\n"

    def render_line(self, y: int) -> Strip:
        """Render one line with source offsets and any active selection.

        Prepared rows acquire Textual caches only when visible. Source offsets
        let Screen translate a drag gesture into text offsets.
        """
        source_y = self.scroll_offset.y + y
        width = self.scrollable_content_region.width
        if source_y >= len(self.lines):
            return Strip.blank(width, self.rich_style)
        key = source_y, self.scroll_offset.x, width
        if key in self._line_cache:
            line = self._line_cache[key]
        else:
            row = self.lines[source_y]
            line = row.to_strip() if isinstance(row, PreparedLine) else row
            line = line.crop_extend(key[1], key[1] + width, self.rich_style)
            self._line_cache[key] = line
        line = line.apply_style(self.rich_style)
        selection = self.text_selection
        if selection is not None and (span := selection.get_span(source_y)) is not None:
            line = _apply_selection_style(
                line,
                *span,
                self.screen.get_component_rich_style("screen--selection", partial=True),
            )
        return line.apply_offsets(0, source_y)

    def notify_style_update(self) -> None:
        super().notify_style_update()
        self._line_cache.clear()

    def get_content_width(self, container: Size, viewport: Size) -> int:
        return self.virtual_size.width if self._size_known else container.width

    def on_click(self, event: events.Click) -> None:
        if event.button != 1 or event.style is None:
            return
        trace_index = event.style.meta.get(_TRACE_ENTRY_META)
        if isinstance(trace_index, int):
            self.post_message(self.ToggleTraceRequested(trace_index))
            event.stop()
            return
        entry_index = event.style.meta.get(_REASONING_ENTRY_META)
        if isinstance(entry_index, int):
            self.post_message(self.ToggleReasoningRequested(entry_index))
            event.stop()
            return
        tool_entry_index = event.style.meta.get(_TOOL_NAVIGATION_META)
        if isinstance(tool_entry_index, int) and 0 <= tool_entry_index < len(self._entries):
            entry = self._entries[tool_entry_index]
            if entry.run_id is not None and entry.tool is not None:
                self.post_message(
                    ToolNavigationRequested(
                        parent_run_id=entry.run_id,
                        parent_tool_call_id=entry.tool.tool_call_id,
                        parent_batch_id=entry.tool.batch_id,
                    )
                )
                event.stop()
        elif event.style.link:
            self.app.open_url(event.style.link)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 3 or event.style is None or self.screen.get_selected_text() is not None:
            return
        message_entry_index = event.style.meta.get(_MESSAGE_META)
        if not isinstance(message_entry_index, int) or not 0 <= message_entry_index < len(
            self._entries
        ):
            return
        entry = self._entries[message_entry_index]
        if entry.kind not in {"user", "scaffold", "command"}:
            return
        event.prevent_default()
        event.stop()
        self.post_message(MessageCopyRequested(entry.text))

    def on_mouse_move(self, event: events.MouseMove) -> None:
        hovered_message = None
        hovered_tool = None
        hovered_reasoning = None
        hovered_trace = None
        if event.style is not None:
            message_entry_index = event.style.meta.get(_MESSAGE_META)
            if isinstance(message_entry_index, int):
                hovered_message = message_entry_index
            tool_entry_index = event.style.meta.get(_TOOL_NAVIGATION_META)
            if isinstance(tool_entry_index, int):
                hovered_tool = tool_entry_index
            reasoning_entry_index = event.style.meta.get(_REASONING_ENTRY_META)
            if isinstance(reasoning_entry_index, int):
                hovered_reasoning = reasoning_entry_index
            trace_index = event.style.meta.get(_TRACE_ENTRY_META)
            if isinstance(trace_index, int):
                hovered_trace = trace_index
        self._set_hovered_entry("_hovered_message_entry_index", hovered_message)
        self._set_hovered_entry("_hovered_tool_entry_index", hovered_tool)
        self._set_hovered_entry("_hovered_reasoning_entry_index", hovered_reasoning)
        self._set_hovered_entry("_hovered_trace_entry_index", hovered_trace)

    def on_leave(self, _event: events.Leave) -> None:
        self._set_hovered_entry("_hovered_message_entry_index", None)
        self._set_hovered_entry("_hovered_tool_entry_index", None)
        self._set_hovered_entry("_hovered_reasoning_entry_index", None)
        self._set_hovered_entry("_hovered_trace_entry_index", None)

    def _set_hovered_entry(self, attribute: str, entry_index: int | None) -> None:
        previous = getattr(self, attribute)
        if previous == entry_index:
            return
        setattr(self, attribute, entry_index)
        for changed in (previous, entry_index):
            if changed is not None and 0 <= changed < len(self._entries):
                self._rerender_entry(changed)

    def replace(self, entries: Iterable[TranscriptEntry]) -> None:
        self._hovered_trace_entry_index = None
        self._trace_collapsed_by_run.clear()
        self._hovered_message_entry_index = None
        self._hovered_tool_entry_index = None
        self._hovered_reasoning_entry_index = None
        self._reasoning_expanded_by_entry.clear()
        if self._reset_buffer(list(entries)):
            self._render_entries()
        elif self._entries:
            self.call_after_refresh(self._render_entries)

    def take_state(self) -> ConversationHistoryState | None:
        """Detach the current rendered history for reuse by its owning Session."""

        if not self._entries:
            return None
        hovered_indexes = {
            index
            for index in (
                self._hovered_message_entry_index,
                self._hovered_tool_entry_index,
                self._hovered_reasoning_entry_index,
                self._hovered_trace_entry_index,
            )
            if index is not None
        }
        self._hovered_message_entry_index = None
        self._hovered_tool_entry_index = None
        self._hovered_reasoning_entry_index = None
        self._hovered_trace_entry_index = None
        if hovered_indexes and not all(self._rerender_entry(index) for index in hovered_indexes):
            self._render_entries()
        state = ConversationHistoryState(
            entries=self._entries,
            pieces=self._pieces,
            piece_line_starts=self._piece_line_starts,
            piece_indexes_by_entry=self._piece_indexes_by_entry,
            user_lines=self._user_lines,
            lines=self.lines,
            widest_line_width=self._widest_line_width,
            render_width=self._render_width,
            rendered_count=0 if self._prefix_pending else self._rendered_count,
            reasoning_expanded=self._reasoning_expanded,
            reasoning_expanded_by_entry=self._reasoning_expanded_by_entry,
            theme=self._render_theme or theme_for(self),
            collapse_traces=self._collapse_traces,
            trace_collapsed_by_run=self._trace_collapsed_by_run,
        )
        self._content_revision += 1
        self._rendered_count = 0
        self._prefix_pending = 0
        self._entries = []
        self._prepared_reasoning.clear()
        self._pending_history_state = None
        self._pieces = []
        self._piece_line_starts = []
        self._piece_indexes_by_entry = {}
        self._tool_entry_indexes = {}
        self._user_lines = []
        self.lines = []
        self._widest_line_width = 0
        self._render_width = 0
        self._reasoning_expanded_by_entry = {}
        self._trace_collapsed_by_run = {}
        self._trace_run_by_entry = {}
        self._trace_summary_indexes = {}
        self._open_trace_entries = {}
        self._visibility_pending = False
        self._line_cache.clear()
        self.virtual_size = Size(0, 0)
        self.display = False
        return state

    async def load(
        self,
        entries: Iterable[TranscriptEntry],
        *,
        reusable_state: ConversationHistoryState | None = None,
        wait_for_render: bool = True,
    ) -> None:
        """Install canonical entries; the render owner prepares their row geometry."""
        self._content_revision += 1
        self._prefix_pending = 0
        self._prepared_reasoning.clear()
        self._pending_history_state = None
        self._visibility_pending = False
        self._render_failed = False
        self._hovered_message_entry_index = None
        self._hovered_tool_entry_index = None
        self._hovered_reasoning_entry_index = None
        self._hovered_trace_entry_index = None
        self._trace_collapsed_by_run = (
            dict(reusable_state.trace_collapsed_by_run) if reusable_state is not None else {}
        )
        materialized = list(entries)
        rendered = self._restore_state(reusable_state, materialized)
        if rendered is None:
            self._reasoning_expanded_by_entry.clear()
            self._reset_buffer(materialized)
            if not self._size_known or self.scrollable_content_region.width <= 0:
                self._pending_history_state = reusable_state
        else:
            self._rendered_count = rendered
        self._full_render_pending = False
        self._visibility_pending = rendered is not None and any(
            self._entry_needs_render(index) and not self._trace_entry_hidden(index)
            for index in range(self._rendered_count)
        )
        if self._rendered_count < len(self._entries) or self._visibility_pending:
            self._schedule_buffer_render(rebuild=False)
        if wait_for_render:
            await self.wait_until_rendered()

    def _restore_state(
        self,
        state: ConversationHistoryState | None,
        entries: list[TranscriptEntry],
    ) -> int | None:
        """Consume compatible state and return the first unrendered entry."""

        width = self.scrollable_content_region.width
        if (
            state is None
            or not self._size_known
            or width <= 0
            or state.render_width != width
            or state.reasoning_expanded != self._reasoning_expanded
            or state.theme != theme_for(self)
        ):
            return None

        shared = 0
        for cached, current in zip(state.entries[: state.rendered_count], entries, strict=False):
            if not self._same_entry_boundary(cached, current):
                break
            shared += 1

        self._entries = state.entries
        self._refresh_traces()
        self._pieces = state.pieces
        self._piece_line_starts = state.piece_line_starts
        self._piece_indexes_by_entry = state.piece_indexes_by_entry
        self._user_lines = state.user_lines
        self.lines = state.lines
        self._widest_line_width = state.widest_line_width
        self._render_width = state.render_width
        self._render_theme = state.theme
        self._rendered_count = min(shared, state.rendered_count)
        self._reasoning_expanded_by_entry = state.reasoning_expanded_by_entry

        if shared < len(self._entries):
            self._entries = self._entries[:shared]
            self._pieces = [piece for piece in self._pieces if piece.entry_index < shared]
            self._reasoning_expanded_by_entry = {
                index: expanded
                for index, expanded in self._reasoning_expanded_by_entry.items()
                if index < shared
            }
            self._rebuild_lines()

        cached_prefix = self._entries
        changed_indexes: list[int] = []
        merged_prefix: list[TranscriptEntry] = []
        for index, (cached, current) in enumerate(
            zip(cached_prefix, entries[:shared], strict=True)
        ):
            merged = current
            if current.kind == "tool" and current.agent is None and cached.agent is not None:
                merged = replace(current, agent=cached.agent)
            merged_prefix.append(merged)
            if merged != cached:
                changed_indexes.append(index)
        self._entries = [*merged_prefix, *entries[shared:]]
        self._refresh_traces()
        self._tool_entry_indexes = {
            (entry.run_id, entry.tool.batch_id, entry.tool.tool_call_id): index
            for index, entry in enumerate(self._entries)
            if entry.tool is not None
        }
        for index in changed_indexes:
            if not self._rerender_entry(index):
                return None
        if self._trace_summary_indexes or state.collapse_traces != self._collapse_traces:
            self._rebuild_lines()
            for index in self._trace_summary_indexes.values():
                self._rerender_entry(index)
        self._line_cache.clear()
        self.display = bool(self._entries)
        self.virtual_size = Size(self._widest_line_width, len(self.lines))
        self.refresh(layout=True)
        return shared

    @staticmethod
    def _same_entry_boundary(
        cached: TranscriptEntry,
        current: TranscriptEntry,
    ) -> bool:
        """Compare immutable position identity, excluding mutable projections."""

        if cached.kind != current.kind:
            return False
        if cached.kind == "tool":
            return (
                cached.run_id == current.run_id
                and cached.tool is not None
                and current.tool is not None
                and cached.tool.tool_call_id == current.tool.tool_call_id
                and cached.tool.batch_id == current.tool.batch_id
            )
        if cached.kind == "run":
            return cached.run_id == current.run_id
        return cached == current

    def _write_entries(self, start: int, stop: int) -> None:
        """Prepare visible entries[start:stop] and publish their geometry."""
        for index in range(start, stop):
            if self._trace_entry_hidden(index):
                continue
            entry = self._entries[index]
            previous_kind = self._entries[index - 1].kind if index > 0 else None
            self._write_entry(
                entry,
                previous_kind=previous_kind,
                entry_index=index,
                sync_size=False,
            )
        self._sync_virtual_size()

    def append(
        self,
        entry: TranscriptEntry,
        *,
        reasoning_expanded: bool | None = None,
        reasoning_layout: ReasoningLayout | None = None,
    ) -> None:
        index = self._register_entry(entry, reasoning_expanded=reasoning_expanded)
        if reasoning_layout is not None:
            self._prepared_reasoning[index] = reasoning_layout
        self._render_append()

    def append_many(self, entries: Iterable[TranscriptEntry]) -> None:
        """Register content before scheduling any expensive layout work."""
        for entry in entries:
            self._register_entry(entry)
        self._render_append()

    def prepend(self, entries: tuple[TranscriptEntry, ...]) -> None:
        """Register an older page; the existing render owner fills the prefix."""
        if not entries:
            return
        self._content_revision += 1
        for attribute in (
            "_hovered_message_entry_index",
            "_hovered_tool_entry_index",
            "_hovered_reasoning_entry_index",
            "_hovered_trace_entry_index",
        ):
            self._set_hovered_entry(attribute, None)
        offset = len(entries)
        self._prepared_reasoning = {
            index + offset: layout for index, layout in self._prepared_reasoning.items()
        }
        previous_traces = set(self._trace_summary_indexes)
        self._entries = [*entries, *self._entries]
        self._tool_entry_indexes = {
            (entry.run_id, entry.tool.batch_id, entry.tool.tool_call_id): index
            for index, entry in enumerate(self._entries)
            if entry.tool is not None
        }
        self._refresh_traces()
        self._reasoning_expanded_by_entry = {
            index + offset: expanded
            for index, expanded in self._reasoning_expanded_by_entry.items()
        }
        if self._rendered_count:
            self._prefix_pending += offset
            self._rendered_count += offset
        _shift_piece_indexes(self._pieces, offset)
        self._rebuild_lines()
        for run_id in self._trace_summary_indexes.keys() - previous_traces:
            # An older page may introduce the first trace for a cached reply.
            # Its existing summary must gain the disclosure target immediately.
            self._rerender_entry(self._trace_summary_indexes[run_id])
        self.display = True
        self._schedule_buffer_render(rebuild=False)

    def _render_append(self) -> None:
        if self._render_failed:
            return
        self.display = bool(self._entries)
        remaining = tuple(self._entries[self._rendered_count :])
        if (
            not self._render_active
            and not self._prefix_pending
            and self._size_known
            and self.scrollable_content_region.width > 0
            and not self._requires_background_render(remaining)
        ):
            self._write_entries(self._rendered_count, len(self._entries))
            self._rendered_count = len(self._entries)
            self._render_width = self.scrollable_content_region.width
            self._render_theme = theme_for(self)
        elif remaining:
            self._schedule_buffer_render(rebuild=False)

    def _register_entry(
        self,
        entry: TranscriptEntry,
        *,
        reasoning_expanded: bool | None = None,
    ) -> int:
        entry_index = len(self._entries)
        self._entries.append(entry)
        self._track_trace_entry(entry_index, entry)
        if entry.tool is not None:
            self._tool_entry_indexes[
                (entry.run_id, entry.tool.batch_id, entry.tool.tool_call_id)
            ] = entry_index
        if (
            entry.reasoning
            and reasoning_expanded is not None
            and reasoning_expanded != self._reasoning_expanded
        ):
            self._reasoning_expanded_by_entry[entry_index] = reasoning_expanded
        return entry_index

    @staticmethod
    def _requires_background_render(entries: tuple[TranscriptEntry, ...]) -> bool:
        if len(entries) > 1:
            return True
        if any(entry.tool is not None for entry in entries):
            return True
        return sum(len(entry.text) + len(entry.reasoning) for entry in entries) >= (
            _BACKGROUND_MARKDOWN_CHARS
        )

    async def wait_until_rendered(self) -> None:
        """Wait for this content's pending layout, without owning its lifetime."""
        while self.is_mounted and not self._render_failed:
            if self.render_complete:
                return
            self._schedule_buffer_render(rebuild=False)
            await self._render_idle.wait()
            if self.render_complete:
                return
            if not self.is_mounted or self._render_failed:
                break
            await self.wait_for_refresh()
        raise RuntimeError("Session history layout did not complete")

    def tool_activity(
        self,
        run_id: str | None,
        batch_id: str | None,
        tool_call_id: str,
    ) -> RuntimeToolCallActivity | None:
        index = self._tool_entry_indexes.get((run_id, batch_id, tool_call_id))
        return self._entries[index].tool if index is not None else None

    def update_tools(
        self,
        tools: Iterable[RuntimeToolCallActivity],
        *,
        run_id: str | None,
    ) -> frozenset[str]:
        updates = {tool.tool_call_id: tool for tool in tools}
        if not updates:
            return frozenset()
        matched: set[str] = set()
        changed_indexes: list[int] = []
        for tool_call_id, tool in updates.items():
            index = self._tool_entry_indexes.get((run_id, tool.batch_id, tool_call_id))
            if index is None:
                continue
            entry = self._entries[index]
            matched.add(tool.tool_call_id)
            if entry.tool != tool:
                self._entries[index] = replace(entry, tool=tool)
                changed_indexes.append(index)
        if changed_indexes:
            if not all(self._rerender_entry(index) for index in changed_indexes):
                self._render_entries()
        return frozenset(matched)

    def update_agents(self, agents: Iterable[AgentTreeNode]) -> None:
        changed_indexes: list[int] = []
        for agent in agents:
            if agent.parent_tool_call_id is None:
                continue
            index = self._tool_entry_indexes.get(
                (agent.parent_run_id, agent.parent_batch_id, agent.parent_tool_call_id)
            )
            if index is None:
                continue
            entry = self._entries[index]
            if entry.agent == agent:
                continue
            self._entries[index] = replace(entry, agent=agent)
            changed_indexes.append(index)
        if changed_indexes:
            if not all(self._rerender_entry(index) for index in changed_indexes):
                self._render_entries()

    def refresh_theme(self) -> None:
        self._render_failed = False
        self._schedule_buffer_render()

    def _schedule_buffer_render(self, *, rebuild: bool = True) -> None:
        """Coalesce theme and geometry changes through the same render owner."""

        if rebuild:
            self._render_revision += 1
            self._full_render_pending = bool(self._entries)
        if not self._entries or not self.is_mounted or self._render_active or self._render_failed:
            return
        self._render_active = True
        self._render_idle.clear()
        _worker: Worker[None] = self.run_worker(
            self._refresh_buffer_async,
            group="conversation-history",
        )

    async def wait_for_theme_refresh(self) -> None:
        """Wait until the latest coalesced theme render reaches the visible buffer."""

        await self._render_idle.wait()

    async def _refresh_buffer_async(self) -> None:
        """Own cold, incremental, theme and geometry rendering in one worker."""
        try:
            while self._entries:
                width = self.scrollable_content_region.width
                if width <= 0 or not self._size_known:
                    return
                if self._pending_history_state is not None:
                    state, self._pending_history_state = self._pending_history_state, None
                    restored = self._restore_state(state, self._entries)
                    if restored is not None:
                        self._rendered_count = restored
                        self._visibility_pending = any(
                            self._entry_needs_render(index) and not self._trace_entry_hidden(index)
                            for index in range(restored)
                        )
                revision = self._content_revision, self._render_revision
                rebuild = self._full_render_pending or (
                    self._rendered_count > 0 and width != self._render_width
                )
                prefix = self._prefix_pending
                visibility = self._visibility_pending
                start = 0 if rebuild or prefix or visibility else self._rendered_count
                stop = (
                    min(prefix + 1, self._rendered_count)
                    if prefix and not rebuild and not visibility
                    else len(self._entries)
                )
                if start == stop:
                    return
                requests = self._render_requests(
                    tuple(self._entries[start:stop]),
                    start_index=start,
                    previous_kind=self._entries[start - 1].kind if start else None,
                    missing_only=visibility and not rebuild and not prefix,
                )
                console = self.app.console
                try:
                    # A restored window is one snapshot, not a stream of old
                    # messages. Prepare it in cancellable chunks, then publish
                    # its complete geometry so the first frame can show its end.
                    rendered: list[_RenderedEntry] = []
                    for offset in range(0, len(requests), _LOAD_CHUNK_ENTRIES):
                        rendered.extend(
                            await asyncio.to_thread(
                                _render_entry_requests,
                                requests[offset : offset + _LOAD_CHUNK_ENTRIES],
                                render_tool=self._render_tool,
                                console=console,
                                options=console.options.update_width(max(width, 1)),
                            )
                        )
                        if (
                            revision != (self._content_revision, self._render_revision)
                            or self.scrollable_content_region.width != width
                        ):
                            break
                except Exception as exc:  # noqa: BLE001 - isolate this history renderer.
                    if revision != (self._content_revision, self._render_revision):
                        continue
                    self._render_failed = True
                    self.notify(
                        f"Cannot render session history ({type(exc).__name__}). Reopen to retry.",
                        severity="error",
                        markup=False,
                    )
                    return
                if (
                    revision != (self._content_revision, self._render_revision)
                    or self.scrollable_content_region.width != width
                    or requests
                    != self._render_requests(
                        tuple(self._entries[start:stop]),
                        start_index=start,
                        previous_kind=self._entries[start - 1].kind if start else None,
                        missing_only=visibility and not rebuild and not prefix,
                    )
                ):
                    continue
                if rebuild:
                    self._reset_buffer(self._entries)
                if visibility and not rebuild and not prefix:
                    changed = {entry.entry_index for entry in rendered}
                    self._pieces = sorted(
                        [
                            *(piece for piece in self._pieces if piece.entry_index not in changed),
                            *(
                                _Piece(
                                    rows,
                                    tag,
                                    entry.entry_index,
                                    entry.reasoning_layout,
                                    entry.reasoning_expanded,
                                    entry.trace_collapsed,
                                )
                                for entry in rendered
                                for rows, tag in entry.pieces
                            ),
                        ],
                        key=lambda piece: piece.entry_index,
                    )
                    self._rebuild_lines()
                    self._rendered_count = stop
                elif prefix and not rebuild:
                    self._pieces = [
                        *(
                            _Piece(
                                rows,
                                tag,
                                entry.entry_index,
                                entry.reasoning_layout,
                                entry.reasoning_expanded,
                                entry.trace_collapsed,
                            )
                            for entry in rendered
                            for rows, tag in entry.pieces
                        ),
                        *(piece for piece in self._pieces if piece.entry_index >= stop),
                    ]
                    self._rebuild_lines()
                else:
                    self._append_rendered_entries(tuple(rendered))
                    self._rendered_count = stop
                if prefix:
                    self._prefix_pending = 0
                    delta = self.entry_line(prefix)
                    self.post_message(self.PrefixRendered(delta, self._content_revision))
                self._render_width = width
                self._render_theme = theme_for(self)
                self._full_render_pending = False
                self._visibility_pending = False
                for entry in rendered:
                    self._prepared_reasoning.pop(entry.entry_index, None)
                self.refresh(layout=True)
                if stop < len(self._entries):
                    await self.wait_for_refresh()
        finally:
            self._render_active = False
            self._render_idle.set()

    async def set_reasoning_expanded(self, expanded: bool, *, above_line: int) -> int:
        """Prepare changed reasoning bodies once, preserving the review anchor."""
        if self._reasoning_expanded == expanded and not self._reasoning_expanded_by_entry:
            return 0
        before = self._height_before(above_line)
        self._reasoning_expanded = expanded
        self._reasoning_expanded_by_entry.clear()
        await self._apply_visibility()
        return self._height_at_boundary(before[1]) - before[0]

    async def toggle_reasoning(self, entry_index: int) -> None:
        """Toggle one reasoning entry without changing the global display default."""
        if not (0 <= entry_index < len(self._entries)):
            return
        entry = self._entries[entry_index]
        if not entry.reasoning:
            return
        expanded = not self.is_reasoning_expanded(entry_index)
        if expanded == self._reasoning_expanded:
            self._reasoning_expanded_by_entry.pop(entry_index, None)
        else:
            self._reasoning_expanded_by_entry[entry_index] = expanded
        await self._apply_visibility()

    def is_reasoning_expanded(self, entry_index: int) -> bool:
        """Return whether a reasoning entry is currently expanded."""
        return self._reasoning_expanded_by_entry.get(
            entry_index,
            self._reasoning_expanded,
        )

    def on_resize(self, event: Resize) -> None:
        size_was_known = self._size_known
        if event.size.width:
            self._size_known = True
        width = self.scrollable_content_region.width
        if (
            self._entries
            and width > 0
            and (
                not size_was_known
                or width != self._render_width
                or self._rendered_count < len(self._entries)
            )
        ):
            self._schedule_buffer_render(
                rebuild=self._rendered_count > 0 and width != self._render_width
            )

    def _reset_buffer(self, entries: list[TranscriptEntry]) -> bool:
        """Reset prepared rows for a full replacement; True when width is known."""
        self._content_revision += 1
        self._rendered_count = 0
        self._prefix_pending = 0
        self._render_failed = False
        self._full_render_pending = False
        self._entries = entries
        self._refresh_traces()
        self._pieces.clear()
        self._prepared_reasoning.clear()
        self._pending_history_state = None
        self._visibility_pending = False
        self._piece_line_starts.clear()
        self._piece_indexes_by_entry.clear()
        self._tool_entry_indexes = {
            (entry.run_id, entry.tool.batch_id, entry.tool.tool_call_id): index
            for index, entry in enumerate(entries)
            if entry.tool is not None
        }
        self._user_lines = []
        self._widest_line_width = 0
        self._line_cache.clear()
        self.lines.clear()
        self.display = bool(entries)
        self.virtual_size = Size(0, 0)
        return self._size_known and self.scrollable_content_region.width > 0

    def _render_entries(self) -> None:
        if self._render_failed:
            return
        width = self.scrollable_content_region.width
        if width <= 0 or not self._size_known:
            return
        if self._requires_background_render(tuple(self._entries)):
            self._schedule_buffer_render()
            return
        self._reset_buffer(self._entries)
        self._write_entries(0, len(self._entries))
        self._render_width = width
        self._render_theme = theme_for(self)
        self._rendered_count = len(self._entries)

    def _write_entry(
        self,
        entry: TranscriptEntry,
        *,
        previous_kind: TranscriptEntryKind | None,
        entry_index: int,
        sync_size: bool = True,
    ) -> None:
        self._append_rendered_entry(
            self._render_one(entry, previous_kind=previous_kind, entry_index=entry_index),
            sync_size=sync_size,
        )

    def _append_rendered_entries(self, entries: tuple[_RenderedEntry, ...]) -> None:
        for entry in entries:
            self._append_rendered_entry(entry, sync_size=False)
        self._sync_virtual_size()

    def _append_rendered_entry(
        self,
        rendered: _RenderedEntry,
        *,
        sync_size: bool,
    ) -> None:
        self._prepared_reasoning.pop(rendered.entry_index, None)
        for rows, tag in rendered.pieces:
            if tag == "user":
                self._user_lines.append(len(self.lines))
            self._piece_line_starts.append(len(self.lines))
            if not self._trace_entry_hidden(rendered.entry_index):
                self._append_rows(rows)
            self._pieces.append(
                _Piece(
                    rows=rows,
                    tag=tag,
                    entry_index=rendered.entry_index,
                    reasoning_layout=rendered.reasoning_layout,
                    reasoning_expanded=rendered.reasoning_expanded,
                    trace_collapsed=rendered.trace_collapsed,
                )
            )
            self._piece_indexes_by_entry.setdefault(rendered.entry_index, []).append(
                len(self._pieces) - 1
            )
        if sync_size:
            self._sync_virtual_size()

    def _rerender_entry(self, entry_index: int) -> bool:
        piece_indexes = self._piece_indexes_by_entry.get(entry_index)
        if not piece_indexes:
            return self._trace_entry_hidden(entry_index)
        entry = self._entries[entry_index]
        if entry.reasoning and self._visibility_pending and self._entry_needs_render(entry_index):
            return False
        previous_kind = self._entries[entry_index - 1].kind if entry_index > 0 else None
        rendered = self._render_one(
            entry,
            previous_kind=previous_kind,
            entry_index=entry_index,
        )
        if len(rendered.pieces) != len(piece_indexes):
            return False
        for piece_index, (new_rows, tag) in zip(piece_indexes, rendered.pieces, strict=True):
            piece = self._pieces[piece_index]
            piece.reasoning_layout = rendered.reasoning_layout
            piece.reasoning_expanded = rendered.reasoning_expanded
            piece.trace_collapsed = rendered.trace_collapsed
            old_length = len(piece.rows)
            start = self._piece_line_starts[piece_index]
            if self._trace_entry_hidden(entry_index):
                piece.rows = new_rows
                piece.tag = tag
                continue
            self.lines[start : start + old_length] = new_rows
            delta = len(new_rows) - old_length
            piece.rows = new_rows
            piece.tag = tag
            if delta:
                for later in range(piece_index + 1, len(self._piece_line_starts)):
                    self._piece_line_starts[later] += delta
                old_end = start + old_length
                self._user_lines = [
                    line + delta if line >= old_end else line for line in self._user_lines
                ]
            self._widest_line_width = max(
                self._widest_line_width,
                new_rows[0].cell_length,
            )
            self.virtual_size = Size(self._widest_line_width, len(self.lines))
            self._line_cache.clear()
            self.refresh(layout=bool(delta))
        return True

    def _render_one(
        self,
        entry: TranscriptEntry,
        *,
        previous_kind: TranscriptEntryKind | None,
        entry_index: int,
    ) -> _RenderedEntry:
        requests = self._render_requests(
            (entry,),
            start_index=entry_index,
            previous_kind=previous_kind,
            visible_only=False,
        )
        console = self.app.console
        return _render_entry_requests(
            requests,
            render_tool=self._render_tool,
            console=console,
            options=console.options.update_width(max(self.scrollable_content_region.width, 1)),
        )[0]

    def _render_requests(
        self,
        entries: tuple[TranscriptEntry, ...],
        *,
        start_index: int,
        previous_kind: TranscriptEntryKind | None,
        missing_only: bool = False,
        visible_only: bool = True,
    ) -> tuple[_EntryRenderRequest, ...]:
        theme = theme_for(self)
        requests: list[_EntryRenderRequest] = []
        previous = previous_kind
        for offset, entry in enumerate(entries):
            entry_index = start_index + offset
            if (visible_only and self._trace_entry_hidden(entry_index)) or (
                missing_only and not self._entry_needs_render(entry_index)
            ):
                previous = entry.kind
                continue
            requests.append(
                _EntryRenderRequest(
                    entry=entry,
                    previous_kind=previous,
                    entry_index=entry_index,
                    theme=theme,
                    reasoning_expanded=self._reasoning_expanded_by_entry.get(
                        entry_index,
                        self._reasoning_expanded,
                    ),
                    reasoning_layout=self._prepared_reasoning.get(entry_index)
                    or next(
                        (
                            self._pieces[index].reasoning_layout
                            for index in self._piece_indexes_by_entry.get(entry_index, ())
                            if self._pieces[index].tag == "reasoning"
                        ),
                        None,
                    ),
                    message_highlighted=self._hovered_message_entry_index == entry_index,
                    tool_highlighted=self._hovered_tool_entry_index == entry_index,
                    reasoning_highlighted=(self._hovered_reasoning_entry_index == entry_index),
                    trace_collapsed=(
                        self.is_trace_collapsed(entry.run_id)
                        if entry.kind == "run" and entry.run_id in self._trace_summary_indexes
                        else None
                    ),
                    trace_highlighted=self._hovered_trace_entry_index == entry_index,
                )
            )
            previous = entry.kind
        return tuple(requests)

    def _entry_needs_render(self, entry_index: int) -> bool:
        indexes = self._piece_indexes_by_entry.get(entry_index)
        entry = self._entries[entry_index]
        trace_collapsed = (
            self.is_trace_collapsed(entry.run_id)
            if entry.kind == "run" and entry.run_id in self._trace_summary_indexes
            else None
        )
        return not indexes or any(
            (
                self._pieces[index].tag == "reasoning"
                and self._pieces[index].reasoning_expanded
                != self.is_reasoning_expanded(entry_index)
            )
            or self._pieces[index].trace_collapsed != trace_collapsed
            for index in indexes
        )

    def _append_rows(self, rows: list[HistoryLine]) -> None:
        self.lines.extend(rows)
        self._widest_line_width = max(
            self._widest_line_width,
            rows[0].cell_length,
        )

    def _sync_virtual_size(self) -> None:
        """Publish one size change after a complete entry or load chunk."""

        if self._trace_visibility_dirty:
            self._rebuild_lines()
            return
        self.virtual_size = Size(self._widest_line_width, len(self.lines))

    def _rebuild_lines(self) -> None:
        """Rebuild visible line offsets from the prepared rows."""
        self._trace_visibility_dirty = False
        self._line_cache.clear()
        lines: list[HistoryLine] = []
        starts: list[int] = []
        user_lines: list[int] = []
        piece_indexes_by_entry: dict[int, list[int]] = {}
        widest = 0
        for piece_index, piece in enumerate(self._pieces):
            starts.append(len(lines))
            piece_indexes_by_entry.setdefault(piece.entry_index, []).append(piece_index)
            if self._trace_entry_hidden(piece.entry_index):
                continue
            if piece.tag == "user":
                user_lines.append(len(lines))
            widest = max(widest, piece.rows[0].cell_length)
            lines.extend(piece.rows)
        self.lines = lines
        self._piece_line_starts = starts
        self._piece_indexes_by_entry = piece_indexes_by_entry
        self._user_lines = user_lines
        self._widest_line_width = widest
        self.virtual_size = Size(widest, len(lines))
        self.refresh(layout=True)

    def _refresh_traces(self) -> None:
        """Find terminal text suffixes at completed Run boundaries.

        Markdown blocks and thinking in the final response stay together.
        Earlier assistant output and tools form the trace; user/scaffold/notice
        entries are never swallowed. A tool or thinking-only ending has no reply.
        """
        previous = self._trace_run_by_entry
        self._trace_run_by_entry = {}
        self._trace_summary_indexes = {}
        self._open_trace_entries = {}
        for index, entry in enumerate(self._entries):
            self._track_trace_entry(index, entry)
        self._trace_visibility_dirty = previous != self._trace_run_by_entry

    def _track_trace_entry(self, index: int, entry: TranscriptEntry) -> None:
        """A terminal Run classifies only its own entries, independent of older history."""
        run_id = entry.run_id
        if run_id is None:
            return
        if entry.kind in {"assistant", "tool"}:
            self._open_trace_entries.setdefault(run_id, []).append(index)
        elif (
            entry.kind == "run" and entry.run is not None and entry.run.duration_seconds is not None
        ):
            indexes = self._open_trace_entries.pop(run_id, [])
            split = len(indexes)
            while split:
                tail = self._entries[indexes[split - 1]]
                if tail.kind != "assistant" or not tail.text.strip():
                    break
                split -= 1
                if tail.reasoning:
                    break
            if split < len(indexes) and split:
                preceding = self._entries[indexes[split - 1]]
                if preceding.kind == "assistant" and preceding.reasoning:
                    split -= 1
            if 0 < split < len(indexes):
                self._trace_summary_indexes[run_id] = index
                self._trace_run_by_entry.update(
                    (trace_index, run_id) for trace_index in indexes[:split]
                )
                self._trace_visibility_dirty = True

    def is_trace_collapsed(self, run_id: str | None) -> bool:
        return (
            self._trace_collapsed_by_run.get(run_id, self._collapse_traces)
            if run_id is not None
            else False
        )

    def _trace_entry_hidden(self, entry_index: int) -> bool:
        run_id = self._trace_run_by_entry.get(entry_index)
        return run_id is not None and self.is_trace_collapsed(run_id)

    def entry_line(self, entry_index: int) -> int:
        indexes = self._piece_indexes_by_entry.get(entry_index)
        if indexes:
            return self._piece_line_starts[indexes[0]]
        return next(
            (
                start
                for piece, start in zip(self._pieces, self._piece_line_starts, strict=True)
                if piece.entry_index > entry_index
            ),
            len(self.lines),
        )

    async def toggle_trace(self, entry_index: int) -> int:
        """Prepare newly visible content before publishing the changed geometry."""
        if not 0 <= entry_index < len(self._entries):
            return 0
        run_id = self._entries[entry_index].run_id
        if run_id is None or self._trace_summary_indexes.get(run_id) != entry_index:
            return 0
        before = self.entry_line(entry_index)
        self._trace_collapsed_by_run[run_id] = not self.is_trace_collapsed(run_id)
        await self._apply_visibility()
        return self.entry_line(entry_index) - before

    async def _apply_visibility(self) -> None:
        if not self._entries:
            return
        self._render_revision += 1
        self._visibility_pending = True
        self._schedule_buffer_render(rebuild=False)
        await self.wait_until_rendered()

    async def set_collapse_traces(self, collapsed: bool, *, above_line: int) -> int:
        """Apply the default while retaining explicit per-Run choices."""
        if self._collapse_traces == collapsed:
            return 0
        before, boundary = self._height_before(above_line)
        self._collapse_traces = collapsed
        await self._apply_visibility()
        return self._height_at_boundary(boundary) - before

    def _height_before(self, above_line: int) -> tuple[int, int]:
        before = sum(
            len(piece.rows)
            for piece, start in zip(self._pieces, self._piece_line_starts, strict=True)
            if start < above_line and not self._trace_entry_hidden(piece.entry_index)
        )
        boundary = next(
            (
                piece.entry_index
                for piece, start in zip(self._pieces, self._piece_line_starts, strict=True)
                if start >= above_line
            ),
            len(self._entries),
        )
        return before, boundary

    def _height_at_boundary(self, boundary: int) -> int:
        return sum(
            len(piece.rows)
            for piece in self._pieces
            if piece.entry_index < boundary and not self._trace_entry_hidden(piece.entry_index)
        )
