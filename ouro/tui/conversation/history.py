"""Conversation history widget and asynchronous render ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import replace

from rich.console import RenderableType
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.events import Resize
from textual.geometry import Size
from textual.message import Message
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import RichLog
from textual.worker import Worker

from ouro.application.contracts.agents import AgentTreeNode
from ouro.application.contracts.execution import RuntimeToolCallActivity
from ouro.tui.conversation.history_models import (
    ConversationHistoryState,
    ToolActivityRenderer,
    TranscriptEntry,
    TranscriptEntryKind,
    _EntryRenderRequest,
    _Piece,
    _PieceTag,
    _RenderedEntry,
)
from ouro.tui.conversation.history_rendering import (
    _apply_selection_style,
    _entry_renderables,
    _reasoning_area,
    _render_entry_requests,
    _render_strips_at_width,
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
        shifted: list[Strip] = []
        for strip in piece.strips:
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
        piece.strips = shifted


class ConversationHistory(RangeSelectionOnly, RichLog, can_focus=False):
    """Line-buffered rendering for immutable persisted conversation history.

    Every entry renders into ``_Piece`` strip spans exactly once — blocks
    frozen mid-stream and whole reloaded messages share the same buffer, so
    streaming and finished output cannot drift apart. Toggling reasoning
    visibility only filters which pieces contribute lines; completed content is
    never re-rendered for a visibility change. Live output remains in regular
    widgets below this buffer because it is still mutable.
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
        super().__init__(
            id="conversation-history",
            wrap=True,
            min_width=1,
            auto_scroll=False,
        )
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
        self._rendered_trace_runs: set[str] = set()
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
        self._theme_render_revision = 0
        self._theme_render_active = False
        self._theme_render_idle = asyncio.Event()
        self._theme_render_idle.set()

    @property
    def entries(self) -> tuple[TranscriptEntry, ...]:
        return tuple(self._entries)

    @property
    def render_complete(self) -> bool:
        return (
            self._rendered_count == len(self._entries)
            and not self._prefix_pending
            and not self._theme_render_active
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

        RichLog emits styled line segments without Textual's source-offset metadata,
        so the Screen cannot otherwise translate a drag gesture into text offsets.
        """
        line = super().render_line(y)
        source_y = self.scroll_offset.y + y
        selection = self.text_selection
        if selection is not None and (span := selection.get_span(source_y)) is not None:
            line = _apply_selection_style(
                line,
                *span,
                self.screen.get_component_rich_style("screen--selection", partial=True),
            )
        return line.apply_offsets(0, source_y)

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
        self._rendered_trace_runs = set()
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
        """Install canonical entries; the render owner fills their strip buffer."""
        self._content_revision += 1
        self._prefix_pending = 0
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
        else:
            self._rendered_count = rendered
        self._full_render_pending = False
        if self._rendered_count < len(self._entries):
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
        self._rendered_trace_runs = {
            entry.run_id
            for entry in state.entries[: state.rendered_count]
            if entry.kind == "run" and entry.run_id is not None
        }
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

    def _write_entries(self, start: int, stop: int) -> int:
        """Render entries[start:stop] into pieces; returns the next index."""
        for index in range(start, stop):
            entry = self._entries[index]
            previous_kind = self._entries[index - 1].kind if index > 0 else None
            self._write_entry(
                entry,
                previous_kind=previous_kind,
                entry_index=index,
                sync_size=False,
            )
        self._sync_virtual_size()
        return stop

    def append(
        self,
        entry: TranscriptEntry,
        *,
        reasoning_expanded: bool | None = None,
    ) -> None:
        self._register_entry(entry, reasoning_expanded=reasoning_expanded)
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
        previous_traces = set(self._trace_summary_indexes)
        self._entries = [*entries, *self._entries]
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
            not self._theme_render_active
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
        if entry.kind == "run":
            self._refresh_traces()
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
        return sum(len(entry.text) for entry in entries) >= _BACKGROUND_MARKDOWN_CHARS

    async def wait_until_rendered(self) -> None:
        """Wait for this content's pending layout, without owning its lifetime."""
        for _attempt in range(4):
            if self.render_complete:
                return
            self._schedule_buffer_render(rebuild=False)
            await self._theme_render_idle.wait()
            if self.render_complete:
                return
            await self.wait_for_refresh()
        raise RuntimeError("Session history layout did not complete")

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
                self._entries[index] = TranscriptEntry(
                    kind="tool",
                    run_id=entry.run_id,
                    tool=tool,
                    agent=entry.agent,
                )
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
            self._entries[index] = TranscriptEntry(
                kind="tool",
                run_id=entry.run_id,
                tool=entry.tool,
                agent=agent,
            )
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
            self._theme_render_revision += 1
            self._full_render_pending = True
        if (
            not self._entries
            or not self.is_mounted
            or self._theme_render_active
            or self._render_failed
        ):
            return
        self._theme_render_active = True
        self._theme_render_idle.clear()
        _worker: Worker[None] = self.run_worker(
            self._refresh_buffer_async,
            group="conversation-history-theme",
        )

    async def wait_for_theme_refresh(self) -> None:
        """Wait until the latest coalesced theme render reaches the visible buffer."""

        await self._theme_render_idle.wait()

    async def _refresh_buffer_async(self) -> None:
        """Own cold, incremental, theme and geometry rendering in one worker."""
        try:
            while self._entries:
                width = self.scrollable_content_region.width
                if width <= 0 or not self._size_known:
                    return
                revision = self._content_revision, self._theme_render_revision
                rebuild = self._full_render_pending or (
                    self._rendered_count > 0 and width != self._render_width
                )
                prefix = self._prefix_pending
                start = 0 if rebuild or prefix else self._rendered_count
                stop = (
                    min(prefix + 1, self._rendered_count)
                    if prefix and not rebuild
                    else len(self._entries)
                )
                if start == stop:
                    return
                requests = self._render_requests(
                    tuple(self._entries[start:stop]),
                    start_index=start,
                    previous_kind=self._entries[start - 1].kind if start else None,
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
                                options=console.options.update_width(max(width, self.min_width)),
                            )
                        )
                        if (
                            revision != (self._content_revision, self._theme_render_revision)
                            or self.scrollable_content_region.width != width
                        ):
                            break
                except Exception as exc:  # noqa: BLE001 - isolate this history renderer.
                    if revision != (self._content_revision, self._theme_render_revision):
                        continue
                    self._render_failed = True
                    self.notify(
                        f"Cannot render session history ({type(exc).__name__}). Reopen to retry.",
                        severity="error",
                        markup=False,
                    )
                    return
                if (
                    revision != (self._content_revision, self._theme_render_revision)
                    or self.scrollable_content_region.width != width
                    or requests
                    != self._render_requests(
                        tuple(self._entries[start:stop]),
                        start_index=start,
                        previous_kind=self._entries[start - 1].kind if start else None,
                    )
                ):
                    continue
                if rebuild:
                    self._reset_buffer(self._entries)
                if prefix and not rebuild:
                    self._pieces = [
                        *(
                            _Piece(strips=strips, tag=tag, entry_index=entry.entry_index)
                            for entry in rendered
                            for strips, tag in entry.pieces
                        ),
                        *(piece for piece in self._pieces if piece.entry_index >= stop),
                    ]
                    self._rebuild_lines()
                else:
                    self._append_rendered_entries(tuple(rendered))
                    self._rendered_count = stop
                if prefix:
                    self._prefix_pending = 0
                    first = self._piece_indexes_by_entry.get(prefix)
                    delta = self._piece_line_starts[first[0]] if first else len(self.lines)
                    self.post_message(self.PrefixRendered(delta, self._content_revision))
                self._render_width = width
                self._render_theme = requests[0].theme
                self._full_render_pending = False
                self.refresh(layout=True)
                if stop < len(self._entries):
                    await self.wait_for_refresh()
        finally:
            self._theme_render_active = False
            self._theme_render_idle.set()

    def set_reasoning_expanded(self, expanded: bool, *, above_line: int) -> int:
        """Switch reasoning pieces between their collapsed and expanded variants.

        Only reasoning pieces re-render — a plain-text render bounded by each
        block; every other piece's strips are reused verbatim. Returns the net
        line delta at indexes below ``above_line`` so the caller can keep the
        viewport anchored over unchanged content.
        """
        if self._reasoning_expanded == expanded and not self._reasoning_expanded_by_entry:
            return 0
        self._reasoning_expanded = expanded
        self._reasoning_expanded_by_entry.clear()
        delta = 0
        position = 0
        changed = False
        for piece in self._pieces:
            visible = not self._trace_entry_hidden(piece.entry_index)
            old_length = len(piece.strips)
            if piece.tag == "reasoning":
                new_strips = self._render_reasoning_piece(piece, expanded=expanded)
                if visible and position < above_line:
                    delta += len(new_strips) - len(piece.strips)
                piece.strips = new_strips
                changed = True
            if visible:
                position += old_length
        if changed:
            self._rebuild_lines()
        return delta

    def toggle_reasoning(self, entry_index: int) -> None:
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
        for piece in self._pieces:
            if piece.tag != "reasoning" or piece.entry_index != entry_index:
                continue
            piece.strips = self._render_reasoning_piece(piece, expanded=expanded)
            self._rebuild_lines()
            return

    def is_reasoning_expanded(self, entry_index: int) -> bool:
        """Return whether a reasoning entry is currently expanded."""
        return self._reasoning_expanded_by_entry.get(
            entry_index,
            self._reasoning_expanded,
        )

    def _render_reasoning_piece(self, piece: _Piece, *, expanded: bool) -> list[Strip]:
        entry_index = piece.entry_index
        entry = self._entries[entry_index]
        top = 1 if (entry_index > 0 and self._entries[entry_index - 1].kind == "tool") else 0
        renderable, _tag = _reasoning_area(
            entry,
            entry_index=entry_index,
            expanded=expanded,
            top=top,
            theme=theme_for(self),
            highlighted=self._hovered_reasoning_entry_index == entry_index,
        )
        return self._render_strips(renderable)

    def on_resize(self, event: Resize) -> None:
        size_was_known = self._size_known
        super().on_resize(event)
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
        """Reset the strip buffer for a full replacement; True when width is known."""
        self._content_revision += 1
        self._rendered_count = 0
        self._prefix_pending = 0
        self._render_failed = False
        self._entries = entries
        self._refresh_traces()
        self._pieces.clear()
        self._rendered_trace_runs.clear()
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
        pieces = tuple(
            (self._render_strips(renderable), tag)
            for renderable, tag in self._entry_pieces(
                entry,
                previous_kind=previous_kind,
                entry_index=entry_index,
            )
        )
        self._append_rendered_entry(
            _RenderedEntry(entry_index=entry_index, pieces=pieces),
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
        entry = self._entries[rendered.entry_index]
        if entry.kind == "run" and entry.run_id in self._trace_summary_indexes:
            assert entry.run_id is not None
            self._rendered_trace_runs.add(entry.run_id)
            self._trace_visibility_dirty = True
        for strips, tag in rendered.pieces:
            if tag == "user":
                self._user_lines.append(len(self.lines))
            self._piece_line_starts.append(len(self.lines))
            if not self._trace_entry_hidden(rendered.entry_index):
                self._append_strips(strips)
            self._pieces.append(
                _Piece(
                    strips=strips,
                    tag=tag,
                    entry_index=rendered.entry_index,
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
            return False
        entry = self._entries[entry_index]
        previous_kind = self._entries[entry_index - 1].kind if entry_index > 0 else None
        renderables = self._entry_pieces(
            entry,
            previous_kind=previous_kind,
            entry_index=entry_index,
        )
        if len(renderables) != len(piece_indexes):
            return False
        for piece_index, (renderable, tag) in zip(piece_indexes, renderables, strict=True):
            piece = self._pieces[piece_index]
            old_length = len(piece.strips)
            start = self._piece_line_starts[piece_index]
            new_strips = self._render_strips(renderable)
            if self._trace_entry_hidden(entry_index):
                piece.strips = new_strips
                piece.tag = tag
                continue
            self.lines[start : start + old_length] = new_strips
            delta = len(new_strips) - old_length
            piece.strips = new_strips
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
                new_strips[0].cell_length,
            )
            self.virtual_size = Size(self._widest_line_width, len(self.lines))
            self._line_cache.clear()
            self.refresh(layout=bool(delta))
        return True

    def _entry_pieces(
        self,
        entry: TranscriptEntry,
        *,
        previous_kind: TranscriptEntryKind | None,
        entry_index: int,
    ) -> list[tuple[RenderableType, _PieceTag]]:
        request = self._render_requests(
            (entry,),
            start_index=entry_index,
            previous_kind=previous_kind,
        )[0]
        return _entry_renderables(request, self._render_tool)

    def _render_requests(
        self,
        entries: tuple[TranscriptEntry, ...],
        *,
        start_index: int,
        previous_kind: TranscriptEntryKind | None,
    ) -> tuple[_EntryRenderRequest, ...]:
        theme = theme_for(self)
        requests: list[_EntryRenderRequest] = []
        previous = previous_kind
        for offset, entry in enumerate(entries):
            entry_index = start_index + offset
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

    def _render_strips(self, renderable: RenderableType) -> list[Strip]:
        """Render one piece at the content width, mirroring ``RichLog.write``."""
        console = self.app.console
        render_width = max(self.scrollable_content_region.width, self.min_width)
        options = console.options.update_width(render_width)
        return _render_strips_at_width(console, options, renderable)

    def _append_strips(self, strips: list[Strip]) -> None:
        self.lines.extend(strips)
        self._widest_line_width = max(
            self._widest_line_width,
            strips[0].cell_length,
        )

    def _sync_virtual_size(self) -> None:
        """Publish one size change after a complete entry or load chunk."""

        if self._trace_visibility_dirty:
            self._rebuild_lines()
            return
        self.virtual_size = Size(self._widest_line_width, len(self.lines))

    def _rebuild_lines(self) -> None:
        """Rebuild the visible line space from pieces; strips are reused as-is."""
        self._trace_visibility_dirty = False
        self._rendered_trace_runs = {
            entry.run_id
            for piece in self._pieces
            if (entry := self._entries[piece.entry_index]).kind == "run"
            and entry.run_id is not None
        }
        self._line_cache.clear()
        lines: list[Strip] = []
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
            widest = max(widest, piece.strips[0].cell_length)
            lines.extend(piece.strips)
        self.lines = lines
        self._piece_line_starts = starts
        self._piece_indexes_by_entry = piece_indexes_by_entry
        self._tool_entry_indexes = {
            (entry.run_id, entry.tool.batch_id, entry.tool.tool_call_id): index
            for index, entry in enumerate(self._entries)
            if entry.tool is not None
        }
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
        pending: dict[str, list[int]] = {}
        previous = self._trace_run_by_entry
        self._trace_run_by_entry = {}
        self._trace_summary_indexes = {}
        for index, entry in enumerate(self._entries):
            run_id = entry.run_id
            if run_id is None:
                continue
            if entry.kind in {"assistant", "tool"}:
                pending.setdefault(run_id, []).append(index)
            elif (
                entry.kind == "run"
                and entry.run is not None
                and entry.run.duration_seconds is not None
            ):
                indexes = pending.pop(run_id, [])
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
        self._trace_visibility_dirty = previous != self._trace_run_by_entry

    def is_trace_collapsed(self, run_id: str | None) -> bool:
        return (
            self._trace_collapsed_by_run.get(run_id, self._collapse_traces)
            if run_id is not None
            else False
        )

    def _trace_entry_hidden(self, entry_index: int) -> bool:
        run_id = self._trace_run_by_entry.get(entry_index)
        return (
            run_id is not None
            and run_id in self._rendered_trace_runs
            and self.is_trace_collapsed(run_id)
        )

    def entry_line(self, entry_index: int) -> int:
        indexes = self._piece_indexes_by_entry.get(entry_index)
        return self._piece_line_starts[indexes[0]] if indexes else len(self.lines)

    def toggle_trace(self, entry_index: int) -> int:
        """Toggle cached pieces and return the summary's displacement."""
        if not 0 <= entry_index < len(self._entries):
            return 0
        run_id = self._entries[entry_index].run_id
        if run_id is None or self._trace_summary_indexes.get(run_id) != entry_index:
            return 0
        before = self.entry_line(entry_index)
        self._trace_collapsed_by_run[run_id] = not self.is_trace_collapsed(run_id)
        self._rebuild_lines()
        self._rerender_entry(entry_index)
        return self.entry_line(entry_index) - before

    def set_collapse_traces(self, collapsed: bool, *, above_line: int) -> int:
        """Apply the default while retaining explicit per-Run choices."""
        if self._collapse_traces == collapsed:
            return 0
        before = sum(
            len(piece.strips)
            for piece, start in zip(self._pieces, self._piece_line_starts, strict=True)
            if start < above_line and not self._trace_entry_hidden(piece.entry_index)
        )
        visible_before = {
            index for index, start in enumerate(self._piece_line_starts) if start < above_line
        }
        self._collapse_traces = collapsed
        self._rebuild_lines()
        after = sum(
            len(piece.strips)
            for index, piece in enumerate(self._pieces)
            if index in visible_before and not self._trace_entry_hidden(piece.entry_index)
        )
        for index in self._trace_summary_indexes.values():
            self._rerender_entry(index)
        return after - before
