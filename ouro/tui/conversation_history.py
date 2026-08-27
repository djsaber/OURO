from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, Literal, Protocol

from markdown_it import MarkdownIt
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.markdown import Markdown as RichMarkdown
from rich.padding import Padding
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax, SyntaxTheme
from rich.text import Text
from textual import events
from textual.events import Resize
from textual.geometry import Size
from textual.message import Message
from textual.selection import Selection
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import RichLog

from ouro.application.agent_models import AgentTreeNode
from ouro.application.supervisor_models import RuntimeToolCallActivity
from ouro.tui.selection import RangeSelectionOnly
from ouro.tui.state import (
    RunPresentation,
    format_compact_count,
    format_duration,
    format_reasoning_label,
)
from ouro.tui.theme import TUI_DESIGN, TuiTheme, theme_for


class ToolActivityRenderer(Protocol):
    def __call__(
        self,
        tool: RuntimeToolCallActivity,
        *,
        frame: int,
        theme: TuiTheme,
        agent: AgentTreeNode | None = None,
    ) -> Text: ...


TranscriptEntryKind = Literal["user", "assistant", "tool", "notice", "compaction", "run"]

_REASONING_ENTRY_META = "ouro_reasoning_entry"
_TOOL_NAVIGATION_META = "ouro_tool_navigation"


class ToolNavigationRequested(Message):
    """A SpawnAgent tool presentation requested its durable child Session."""

    def __init__(self, *, parent_run_id: str, parent_tool_call_id: str) -> None:
        super().__init__()
        self.parent_run_id = parent_run_id
        self.parent_tool_call_id = parent_tool_call_id


class _PanelCodeTheme(SyntaxTheme):
    """Preserve Monokai token colors on OURO's shared panel surface."""

    def __init__(self) -> None:
        self._base = Syntax.get_theme("monokai")

    def get_style_for_token(self, token_type: Any) -> Style:
        return self._base.get_style_for_token(token_type) + Style(bgcolor=TUI_DESIGN.panel)

    def get_background_style(self) -> Style:
        return Style(bgcolor=TUI_DESIGN.panel)


_PANEL_CODE_THEME = _PanelCodeTheme()

# Rich's Markdown renders through this same parser configuration, so block
# boundaries detected for freezing always agree with the rendered structure.
_MARKDOWN_PARSER = MarkdownIt().enable("strikethrough").enable("table")

_ATOMIC_BLOCKS = frozenset({"fence", "hr", "code_block", "html_block"})
_LIVE_MARKDOWN_BOUNDARY_MAX_CHARS = 8 * 1024


def assistant_markdown(text: str) -> RichMarkdown:
    """The one assistant markdown rendering definition.

    Streamed tails, frozen blocks, and reloaded history all pass through this
    factory, so the three paths cannot drift apart stylistically.
    """
    return RichMarkdown(text, code_theme=_PANEL_CODE_THEME)  # type: ignore[arg-type]


def _block_starts(source: str) -> list[int]:
    """Line indexes where complete top-level markdown blocks begin."""
    return [
        token.map[0]
        for token in _MARKDOWN_PARSER.parse(source)
        if token.level == 0
        and token.map is not None
        and (token.type.endswith("_open") or token.type in _ATOMIC_BLOCKS)
    ]


class AssistantMarkdownStream:
    """Incremental markdown splitting for one streaming answer.

    Completed top-level blocks are returned for immutable rendering as soon as
    a following block starts. The trailing block always stays live: later
    fragments can still extend or re-type it (setext underlines, table
    delimiter rows), so freezing it early could lock in a stale structure.
    """

    def __init__(self) -> None:
        self._source_parts: list[str] = []
        self._lines: list[str] = []
        self._pending_parts: list[str] = []
        self._frozen = 0
        self._tail_chars = 0

    @property
    def source(self) -> str:
        return "".join(self._source_parts)

    @property
    def has_source(self) -> bool:
        return bool(self._source_parts)

    @property
    def tail(self) -> str:
        return self._unfrozen()

    def feed(self, text: str) -> tuple[tuple[str, ...], str]:
        """Accumulate a fragment; return newly completed blocks and the live tail."""
        blocks, _ = self.append(text)
        return blocks, self._unfrozen()

    def append(self, text: str) -> tuple[tuple[str, ...], bool]:
        """Append a fragment without materializing the whole live tail.

        The boolean reports whether completed blocks were removed from the
        live tail, allowing its append-only presentation to reset only at a
        structural Markdown boundary.
        """
        if not text:
            return (), False
        self._source_parts.append(text)
        self._tail_chars += len(text)
        completed_line = self._append_logical_lines(text)
        if not completed_line or self._tail_chars > _LIVE_MARKDOWN_BOUNDARY_MAX_CHARS:
            return (), False

        tail_lines = self._tail_lines()
        starts = _block_starts("".join(tail_lines))
        if len(starts) < 2:
            return (), False
        blocks = tuple("".join(tail_lines[start:end]) for start, end in pairwise(starts))
        frozen_chars = sum(len(line) for line in tail_lines[: starts[-1]])
        self._frozen += starts[-1]
        self._tail_chars -= frozen_chars
        return tuple(block for block in blocks if block.strip()), True

    def close(self) -> tuple[str, ...]:
        """Freeze everything, including the trailing block."""
        tail_lines = self._tail_lines()
        starts = _block_starts("".join(tail_lines))
        boundaries = [*starts, len(tail_lines)]
        blocks = ("".join(tail_lines[start:end]) for start, end in pairwise(boundaries))
        return tuple(block for block in blocks if block.strip())

    def _unfrozen(self) -> str:
        return "".join(self._tail_lines())

    def _tail_lines(self) -> list[str]:
        lines = list(self._lines[self._frozen :])
        if self._pending_parts:
            lines.append("".join(self._pending_parts))
        return lines

    def _append_logical_lines(self, text: str) -> bool:
        """Append source using markdown-it line coordinates in O(new text)."""
        fragments = text.split("\n")
        if len(fragments) == 1:
            self._pending_parts.append(text)
            return False

        first = "".join((*self._pending_parts, fragments[0]))
        self._lines.append(first + "\n")
        self._lines.extend(fragment + "\n" for fragment in fragments[1:-1])
        self._pending_parts = [fragments[-1]] if fragments[-1] else []
        return True


def split_markdown_blocks(text: str) -> tuple[str, ...]:
    """Split a complete markdown document into top-level block slices."""
    stream = AssistantMarkdownStream()
    blocks = list(stream.feed(text)[0])
    blocks.extend(stream.close())
    return tuple(blocks)


def _thought_label(
    reasoning_seconds: float | None,
    *,
    entry_index: int,
    expanded: bool,
) -> Text:
    label = format_reasoning_label("Thought", reasoning_seconds, expanded=expanded)
    return Text(
        label,
        style=Style(
            color=TUI_DESIGN.text_secondary,
            bold=True,
            meta={_REASONING_ENTRY_META: entry_index},
        ),
    )


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
    reasoning: str = ""
    run_id: str | None = None
    reasoning_seconds: float | None = None
    tool: RuntimeToolCallActivity | None = None
    agent: AgentTreeNode | None = None
    run: RunPresentation | None = None
    compaction: CompactionPresentation | None = None
    error: bool = False


@dataclass(frozen=True)
class _UserMessageContent:
    """Render the original user-turn frame without adding a Textual widget."""

    text: str
    accent: str

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        top, right, bottom, left = (1, 2, 1, 2)
        frame_width = max(1, options.max_width)
        interior_width = frame_width - 1
        left_width = min(left, interior_width)
        right_width = min(right, interior_width - left_width)
        content_width = interior_width - left_width - right_width
        content_options = options.update_width(max(1, content_width))
        panel_style = Style(bgcolor=TUI_DESIGN.panel)
        content = Text(self.text, style=TUI_DESIGN.text_highlight)
        lines = console.render_lines(content, content_options, pad=False)
        border = Segment("│", Style(color=self.accent))

        def framed_line(line: list[Segment]) -> RenderResult:
            yield border
            yield Segment(" " * left_width, panel_style)
            if content_width:
                styled_line = list(Segment.apply_style(line, style=panel_style))
                yield from Segment.adjust_line_length(
                    styled_line,
                    content_width,
                    style=panel_style,
                )
            yield Segment(" " * right_width, panel_style)
            yield Segment.line()

        empty_line: list[Segment] = []
        for _ in range(top):
            yield from framed_line(empty_line)
        for line in lines:
            yield from framed_line(line)
        for _ in range(bottom):
            yield from framed_line(empty_line)


@dataclass(frozen=True)
class _ReasoningContent:
    """Render completed reasoning with the same structure as its live view.

    RichLog stores completed transcript entries as renderables rather than child
    widgets. Prefixing after Rich has wrapped the content preserves the border on
    every visual line without adding a widget per historical reasoning block.
    """

    text: str

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        prefix_width = 2
        content_options = options.update_width(max(1, options.max_width - prefix_width))
        content = Text(self.text, style=f"dim italic {TUI_DESIGN.text_muted}")
        lines = console.render_lines(content, content_options, pad=False)
        border = Segment("│", Style(color=TUI_DESIGN.border))
        gap = Segment(" ")
        for line in lines:
            yield border
            yield gap
            yield from line
            yield Segment.line()


def _apply_selection_style(
    strip: Strip,
    start: int,
    end: int,
    style: Style,
) -> Strip:
    """Apply a character-indexed selection style without losing Rich segments."""

    text_length = len(strip.text)
    start = min(max(0, start), text_length)
    end = text_length if end < 0 else min(max(0, end), text_length)
    if end <= start:
        return strip

    segments: list[Segment] = []
    position = 0
    for segment in strip:
        if segment.control:
            segments.append(segment)
            continue

        text = segment.text
        segment_end = position + len(text)
        selected_start = max(start, position)
        selected_end = min(end, segment_end)
        if selected_start >= selected_end:
            segments.append(segment)
        else:
            local_start = selected_start - position
            local_end = selected_end - position
            if local_start:
                segments.append(Segment(text[:local_start], segment.style))
            selected_style = style if segment.style is None else segment.style + style
            segments.append(Segment(text[local_start:local_end], selected_style))
            if local_end < len(text):
                segments.append(Segment(text[local_end:], segment.style))
        position = segment_end
    return Strip(segments, strip.cell_length)


class ReasoningLines(RangeSelectionOnly, Widget):
    """Append-only strip buffer for one streaming plain-text block.

    Completed visual lines render exactly once through the shared reasoning
    renderable. Only the final, not-yet-full visual line re-renders while it
    grows, so even a source line without newlines has bounded per-flush cost.
    """

    can_focus = False

    def __init__(self, *, classes: str = "reasoning-lines") -> None:
        super().__init__(classes=classes)
        self._source_parts: list[str] = []
        self._pending_parts: list[str] = []
        self._line_strips: list[Strip] = []
        self._pending_strips: list[Strip] = []
        self._render_width = 0
        self._deferred_render = False

    @property
    def text(self) -> str:
        """The exact stream source, independent from visual wrapping."""
        return "".join(self._source_parts)

    @property
    def rendered_text(self) -> str:
        return "\n".join(
            strip.text.rstrip() for strip in (*self._line_strips, *self._pending_strips)
        )

    def append(self, fragment: str) -> None:
        if not fragment:
            return
        self._source_parts.append(fragment)
        self._pending_parts.append(fragment)
        self._render_updates()

    def replace(self, text: str) -> None:
        """Replace the source after an infrequent structural boundary."""
        self._source_parts = [text] if text else []
        self._pending_parts = [text] if text else []
        self._line_strips.clear()
        self._pending_strips.clear()
        self._render_updates()
        if not text:
            self.refresh(layout=True)

    def on_resize(self, event: Resize) -> None:
        self._render_updates()

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        # Strips are counted, not measured: auto height stays O(1) while the
        # block grows, so sizing the widget never renders its content.
        if not width:
            return 0
        return len(self._line_strips) + len(self._pending_strips)

    def render_line(self, y: int) -> Strip:
        committed = len(self._line_strips)
        if y < committed:
            line = self._line_strips[y]
        elif y - committed < len(self._pending_strips):
            line = self._pending_strips[y - committed]
        else:
            return Strip.blank(self.size.width or 1)
        # Pre-rendered strips carry neither selection styling nor Textual's
        # source-offset metadata, so both are attached per line here.
        selection = self.text_selection
        if selection is not None and (span := selection.get_span(y)) is not None:
            line = _apply_selection_style(
                line,
                *span,
                self.screen.get_component_rich_style("screen--selection", partial=True),
            )
        return line.apply_offsets(0, y)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        return selection.extract(self.rendered_text), "\n"

    def _render_updates(self) -> None:
        """Render buffered source into strips at the current content width.

        A width change rebuilds every strip (rare); otherwise only newly
        completed visual lines and the trailing partial visual line render, so
        per-flush work stays proportional to the new text.
        """
        width = self.content_region.width
        if width <= 0:
            # Not laid out yet (or collapsed): render once layout provides a
            # width instead of depending on the resize event's timing.
            if not self._deferred_render and self._source_parts:
                self._deferred_render = True
                self.call_after_refresh(self._render_deferred_updates)
            return
        rebuild = width != self._render_width
        if rebuild:
            self._render_width = width
            self._line_strips.clear()
            source = self.text
            self._pending_parts = [source] if source else []
        if not self._pending_parts:
            return

        pending = "".join(self._pending_parts)
        wrapped = self._wrap_source(pending)
        for line in wrapped[:-1]:
            self._line_strips.extend(self._render_source_line(line))
        trailing = wrapped[-1] if wrapped else ""
        self._pending_parts = [trailing] if trailing else []
        self._pending_strips = self._render_source_line(trailing) if trailing else []
        self.refresh(layout=True)

    def _render_deferred_updates(self) -> None:
        self._deferred_render = False
        self._render_updates()

    def _wrap_source(self, source: str) -> list[str]:
        lines = Text(source).wrap(
            self.app.console,
            self._source_wrap_width(),
            overflow="fold",
            no_wrap=False,
        )
        return [line.plain for line in lines]

    def _source_wrap_width(self) -> int:
        return max(1, self._render_width - 2)

    def _render_source_line(self, line: str) -> list[Strip]:
        """Render one pre-wrapped line the way frozen reasoning renders it.

        The line already fits reasoning's content width, so rendering it as a
        separate bordered row matches the equivalent whole-block row.
        """
        console = self.app.console
        options = console.options.update_width(self._render_width)
        segments = console.render(_ReasoningContent(line), options)
        return [
            strip.adjust_cell_length(self._render_width)
            for strip in Strip.from_lines(list(Segment.split_lines(segments)))
        ]


class AssistantTailLines(ReasoningLines):
    """Append-only plain-text presentation for the mutable Markdown tail.

    Markdown syntax remains visible while its structure can still change.
    Once a following block establishes the boundary, immutable history renders
    the completed source exactly once through ``assistant_markdown``.
    """

    def __init__(self) -> None:
        super().__init__(classes="assistant-markdown")

    def _source_wrap_width(self) -> int:
        return self._render_width

    def _render_source_line(self, line: str) -> list[Strip]:
        console = self.app.console
        options = console.options.update_width(self._render_width)
        rendered_lines = console.render_lines(Text(line), options, pad=False)
        if not rendered_lines:
            return [Strip.blank(self._render_width)]
        return [
            Strip(segments).adjust_cell_length(self._render_width) for segments in rendered_lines
        ]


_PieceTag = Literal["body", "user", "reasoning"]

_LOAD_CHUNK_ENTRIES = 50


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


def _summary_line(
    title: str,
    *,
    marker_tone: str,
    details: Iterable[tuple[str, str]],
) -> Text:
    summary = Text()
    summary.append(f"{TUI_DESIGN.brand_mark} ", style=f"bold {marker_tone}")
    summary.append(title, style=f"bold {TUI_DESIGN.text_secondary}")
    for value, tone in details:
        summary.append(TUI_DESIGN.section_separator, style=TUI_DESIGN.separator_tone)
        summary.append(value, style=tone)
    return summary


def _compaction_summary(compaction: CompactionPresentation, theme: TuiTheme) -> Text:
    noun = "message" if compaction.covered_message_count == 1 else "messages"
    details = [
        (
            f"{compaction.covered_message_count} {noun} summarized",
            TUI_DESIGN.text_metadata,
        )
    ]
    if compaction.tokens_reduced is not None:
        details.append(
            (
                f"{format_compact_count(compaction.tokens_reduced)} tokens reduced",
                TUI_DESIGN.text_metadata,
            )
        )
    if compaction.duration_seconds is not None:
        details.append(
            (
                format_duration(compaction.duration_seconds),
                TUI_DESIGN.text_metadata,
            )
        )
    return _summary_line(
        "Context compacted",
        marker_tone=theme.accent,
        details=details,
    )


def _run_summary(run: RunPresentation, theme: TuiTheme) -> Text:
    marker_tone = (
        TUI_DESIGN.failure
        if run.status == "failed"
        else theme.accent
        if run.status == "completed"
        else TUI_DESIGN.text_muted
    )
    details: list[tuple[str, str]] = []
    if run.duration_seconds is not None:
        details.append((format_duration(run.duration_seconds), TUI_DESIGN.text_metadata))
    if run.status != "completed":
        status_tone = TUI_DESIGN.failure if run.status == "failed" else TUI_DESIGN.text_metadata
        details.append((run.status, status_tone))
    if run.status == "failed" and run.error:
        details.append((" ".join(run.error.split()), TUI_DESIGN.failure))
    return _summary_line(
        run.model,
        marker_tone=marker_tone,
        details=details,
    )


def _reasoning_area(
    entry: TranscriptEntry,
    *,
    entry_index: int,
    expanded: bool,
    top: int,
) -> tuple[RenderableType, _PieceTag]:
    """The reasoning area of one entry: label plus the togglable body.

    The area always ends in exactly one blank line — the gap belongs between
    reasoning and its answer whether or not an answer follows.
    """
    label = _thought_label(
        entry.reasoning_seconds,
        entry_index=entry_index,
        expanded=expanded,
    )
    if expanded:
        area: RenderableType = Group(
            label,
            # Keep the border in the label's column. The trailing cell mirrors
            # ReasoningLines' right padding, so live and frozen thoughts retain
            # the same content width and wrapping when the stream is promoted.
            Padding(_ReasoningContent(entry.reasoning), (1, 1, 0, 0)),
        )
    else:
        area = label
    return Padding(area, (top, 2, 1, 2)), "reasoning"


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

    def __init__(
        self,
        render_tool: ToolActivityRenderer,
        *,
        reasoning_expanded: bool = True,
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
        self._tool_entry_indexes: dict[tuple[str | None, str], int] = {}
        self._user_lines: tuple[int, ...] = ()
        self._render_width = 0
        self._reasoning_expanded = reasoning_expanded
        self._reasoning_expanded_by_entry: dict[int, bool] = {}

    @property
    def entries(self) -> tuple[TranscriptEntry, ...]:
        return tuple(self._entries)

    @property
    def user_turns(self) -> tuple[str, ...]:
        return tuple(entry.text for entry in self._entries if entry.kind == "user")

    @property
    def user_lines(self) -> tuple[int, ...]:
        return self._user_lines

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
                    )
                )
                event.stop()
        elif event.style.link:
            self.app.open_url(event.style.link)

    def replace(self, entries: Iterable[TranscriptEntry]) -> None:
        self._reasoning_expanded_by_entry.clear()
        if self._reset_buffer(list(entries)):
            self._render_entries()
        elif self._entries:
            self.call_after_refresh(self._render_entries)

    async def load(self, entries: Iterable[TranscriptEntry]) -> None:
        """Replace the transcript in chunks so loading never blocks the UI thread.

        The status line keeps animating its loading activity while chunks mount;
        each chunk yields one frame in between. Returns only once the entries
        are rendered, preserving the ``load_messages`` contract that callers
        rely on before anchoring the scroll position.
        """
        self._reasoning_expanded_by_entry.clear()
        if not self._reset_buffer(list(entries)):
            if self._entries:
                # Width is unknown (the log was hidden while empty); render as
                # soon as layout provides one instead of leaving callers racing
                # an async mount.
                self.call_after_refresh(self._render_entries)
                await self.wait_until_rendered()
            return
        rendered = 0
        try:
            for start in range(0, len(self._entries), _LOAD_CHUNK_ENTRIES):
                rendered = self._write_entries(
                    start, min(start + _LOAD_CHUNK_ENTRIES, len(self._entries))
                )
                await self.wait_for_refresh()
        except asyncio.CancelledError:
            # Exclusive workers supersede an in-flight session load; finish the
            # remaining entries synchronously so the buffer never rests
            # half-rendered before the cancellation propagates.
            self._write_entries(rendered, len(self._entries))
            raise
        self._render_width = self.scrollable_content_region.width

    def _write_entries(self, start: int, stop: int) -> int:
        """Render entries[start:stop] into pieces; returns the next index."""
        for index in range(start, stop):
            entry = self._entries[index]
            previous_kind = self._entries[index - 1].kind if index > 0 else None
            self._write_entry(entry, previous_kind=previous_kind, entry_index=index)
        return stop

    def append(
        self,
        entry: TranscriptEntry,
        *,
        reasoning_expanded: bool | None = None,
    ) -> None:
        entry_index = len(self._entries)
        self._entries.append(entry)
        if entry.tool is not None:
            self._tool_entry_indexes[(entry.run_id, entry.tool.tool_call_id)] = entry_index
        if (
            entry.reasoning
            and reasoning_expanded is not None
            and reasoning_expanded != self._reasoning_expanded
        ):
            self._reasoning_expanded_by_entry[entry_index] = reasoning_expanded
        self.display = True
        if self._size_known and self.scrollable_content_region.width > 0:
            previous_kind = self._entries[-2].kind if len(self._entries) > 1 else None
            self._write_entry(
                entry,
                previous_kind=previous_kind,
                entry_index=entry_index,
            )
        else:
            self.call_after_refresh(self._render_entries)

    async def wait_until_rendered(self) -> None:
        """Render the initial line buffer once layout geometry becomes available."""

        # A deferred render may run while an ancestor is still hidden and has
        # no usable geometry. Retrying the render here makes this method own its
        # contract instead of relying on a later Resize event that may not fire.
        for attempt in range(4):
            if not self._entries or self.lines:
                return
            self._render_entries()
            if self.lines:
                return
            if attempt < 3:
                await self.wait_for_refresh()

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
            index = self._tool_entry_indexes.get((run_id, tool_call_id))
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
                (agent.parent_run_id, agent.parent_tool_call_id)
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
        if self._entries:
            self._render_entries()

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
            if piece.tag == "reasoning":
                new_strips = self._render_reasoning_piece(piece, expanded=expanded)
                if position < above_line:
                    delta += len(new_strips) - len(piece.strips)
                piece.strips = new_strips
                changed = True
            position += len(piece.strips)
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
        )
        return self._render_strips(renderable)

    def on_resize(self, event: Resize) -> None:
        size_was_known = self._size_known
        super().on_resize(event)
        width = self.scrollable_content_region.width
        if self._entries and width > 0 and (not size_was_known or width != self._render_width):
            self._render_entries()

    def _reset_buffer(self, entries: list[TranscriptEntry]) -> bool:
        """Reset the strip buffer for a full replacement; True when width is known."""
        self._entries = entries
        self._pieces.clear()
        self._piece_line_starts.clear()
        self._piece_indexes_by_entry.clear()
        self._tool_entry_indexes = {
            (entry.run_id, entry.tool.tool_call_id): index
            for index, entry in enumerate(entries)
            if entry.tool is not None
        }
        self._user_lines = ()
        self._widest_line_width = 0
        self._line_cache.clear()
        self.lines.clear()
        self.display = bool(entries)
        self.virtual_size = Size(0, 0)
        return self._size_known and self.scrollable_content_region.width > 0

    def _render_entries(self) -> None:
        width = self.scrollable_content_region.width
        if width <= 0 or not self._size_known:
            return
        self._reset_buffer(self._entries)
        self._write_entries(0, len(self._entries))
        self._render_width = width

    def _write_entry(
        self,
        entry: TranscriptEntry,
        *,
        previous_kind: TranscriptEntryKind | None,
        entry_index: int,
    ) -> None:
        for renderable, tag in self._entry_pieces(
            entry,
            previous_kind=previous_kind,
            entry_index=entry_index,
        ):
            strips = self._render_strips(renderable)
            if tag == "user":
                self._user_lines = (*self._user_lines, len(self.lines))
            self._piece_line_starts.append(len(self.lines))
            self._append_strips(strips)
            self._pieces.append(
                _Piece(
                    strips=strips,
                    tag=tag,
                    entry_index=entry_index,
                )
            )
            self._piece_indexes_by_entry.setdefault(entry_index, []).append(len(self._pieces) - 1)

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
            self.lines[start : start + old_length] = new_strips
            delta = len(new_strips) - old_length
            piece.strips = new_strips
            piece.tag = tag
            if delta:
                for later in range(piece_index + 1, len(self._piece_line_starts)):
                    self._piece_line_starts[later] += delta
                old_end = start + old_length
                self._user_lines = tuple(
                    line + delta if line >= old_end else line for line in self._user_lines
                )
            self._widest_line_width = max(
                self._widest_line_width,
                max(strip.cell_length for strip in new_strips),
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
        theme = theme_for(self)
        if entry.kind == "user":
            return [
                (
                    Padding(_UserMessageContent(entry.text, theme.accent), (1, 2, 2, 2)),
                    "user",
                )
            ]
        if entry.kind == "tool":
            assert entry.tool is not None
            rendered = self._render_tool(
                entry.tool,
                frame=0,
                theme=theme,
                agent=entry.agent,
            )
            if entry.tool.tool_name == "SpawnAgent" and entry.run_id is not None:
                rendered.stylize(
                    Style(meta={_TOOL_NAVIGATION_META: entry_index}),
                    0,
                    len(rendered),
                )
            return [
                (
                    Padding(
                        rendered,
                        (0, 0, 0, 2),
                    ),
                    "body",
                )
            ]
        if entry.kind == "notice":
            tone = TUI_DESIGN.critical if entry.error else theme.accent
            top = 1 if previous_kind == "tool" else 0
            return [(Padding(Text(entry.text, style=tone), (top, 1, 1, 1)), "body")]
        if entry.kind == "compaction":
            assert entry.compaction is not None
            top = 1 if previous_kind == "tool" else 0
            summary = _compaction_summary(entry.compaction, theme)
            return [(Padding(summary, (top, 1, 1, 2)), "body")]
        if entry.kind == "run":
            assert entry.run is not None
            summary = _run_summary(entry.run, theme)
            return [(Padding(summary, (0, 1, 1, 2)), "body")]

        pieces: list[tuple[RenderableType, _PieceTag]] = []
        top = 1 if previous_kind == "tool" else 0
        if entry.reasoning:
            pieces.append(
                _reasoning_area(
                    entry,
                    entry_index=entry_index,
                    expanded=self.is_reasoning_expanded(entry_index),
                    top=top,
                )
            )
        if entry.text:
            text_top = 0 if entry.reasoning else top
            pieces.append(
                (
                    Padding(assistant_markdown(entry.text), (text_top, 2, 1, 2)),
                    "body",
                )
            )
        return pieces

    def _render_strips(self, renderable: RenderableType) -> list[Strip]:
        """Render one piece at the content width, mirroring ``RichLog.write``."""
        console = self.app.console
        render_width = max(self.scrollable_content_region.width, self.min_width)
        options = console.options.update_width(render_width)
        segments = console.render(renderable, options)
        strips = [
            strip.adjust_cell_length(render_width)
            for strip in Strip.from_lines(list(Segment.split_lines(segments)))
        ]
        return strips or [Strip.blank(render_width)]

    def _append_strips(self, strips: list[Strip]) -> None:
        self.lines.extend(strips)
        self._widest_line_width = max(
            self._widest_line_width,
            max(strip.cell_length for strip in strips),
        )
        self.virtual_size = Size(self._widest_line_width, len(self.lines))

    def _rebuild_lines(self) -> None:
        """Rebuild the visible line space from pieces; strips are reused as-is."""
        self._line_cache.clear()
        lines: list[Strip] = []
        starts: list[int] = []
        user_lines: list[int] = []
        widest = 0
        for piece in self._pieces:
            starts.append(len(lines))
            if piece.tag == "user":
                user_lines.append(len(lines))
            widest = max(widest, max(strip.cell_length for strip in piece.strips))
            lines.extend(piece.strips)
        self.lines = lines
        self._piece_line_starts = starts
        self._user_lines = tuple(user_lines)
        self._widest_line_width = widest
        self.virtual_size = Size(widest, len(lines))
        self.refresh()
