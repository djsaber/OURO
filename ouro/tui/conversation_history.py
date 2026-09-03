from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import Literal, Protocol

from markdown_it import MarkdownIt
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.padding import Padding
from rich.segment import Segment
from rich.style import Style
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
from ouro.tui.markdown import assistant_markdown
from ouro.tui.selection import RangeSelectionOnly
from ouro.tui.state import (
    RunPresentation,
    format_compact_count,
    format_duration,
    format_reasoning_label_parts,
)
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, theme_for


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


# Rich's Markdown renders through this same parser configuration, so block
# boundaries detected for freezing always agree with the rendered structure.
_MARKDOWN_PARSER = MarkdownIt().enable("strikethrough").enable("table")

_ATOMIC_BLOCKS = frozenset({"fence", "hr", "code_block", "html_block"})
_LIVE_MARKDOWN_BOUNDARY_MAX_CHARS = 8 * 1024
_BACKGROUND_MARKDOWN_CHARS = 2 * 1024


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

    def should_parse_in_background(self, text: str) -> bool:
        """Return whether this append would perform frame-sized boundary parsing."""

        tail_chars = self._tail_chars + len(text)
        return (
            "\n" in text
            and _BACKGROUND_MARKDOWN_CHARS <= tail_chars <= _LIVE_MARKDOWN_BOUNDARY_MAX_CHARS
        )

    @property
    def should_close_in_background(self) -> bool:
        return self._tail_chars >= _BACKGROUND_MARKDOWN_CHARS

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
    theme: TuiTheme,
    highlighted: bool = False,
) -> Text:
    label = _reasoning_label_text(
        "Thought",
        reasoning_seconds,
        expanded=expanded,
        theme=theme,
    )
    if highlighted:
        label.stylize(theme.text_highlight)
    label.stylize(Style(meta={_REASONING_ENTRY_META: entry_index}))
    return label


def _reasoning_label_text(
    label: str,
    duration_seconds: float | None,
    *,
    expanded: bool,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> Text:
    title, duration = format_reasoning_label_parts(
        label,
        duration_seconds,
        expanded=expanded,
        theme=theme,
    )
    text = Text(title, style=Style(color=theme.text_secondary))
    if duration is not None:
        text.append(
            theme.inline_separator,
            style=Style(color=theme.separator_tone),
        )
        text.append(duration, style=Style(color=theme.text_faint))
    return text


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
    theme: TuiTheme

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
        panel_style = Style(bgcolor=self.theme.panel)
        content = Text(self.text, style=self.theme.text_highlight)
        lines = console.render_lines(content, content_options, pad=False)
        border = Segment("│", Style(color=self.theme.accent))

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
    theme: TuiTheme = DEFAULT_TUI_THEME

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        prefix_width = 2
        content_options = options.update_width(max(1, options.max_width - prefix_width))
        content = Text(self.text, style=f"dim italic {self.theme.text_muted}")
        lines = console.render_lines(content, content_options, pad=False)
        border = Segment("│", Style(color=self.theme.border))
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

    def refresh_theme(self) -> None:
        source = self.text
        self._line_strips.clear()
        self._pending_strips.clear()
        self._pending_parts = [source] if source else []
        self._render_updates()

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
            return Strip.blank(
                self.size.width or 1,
                Style(color=theme_for(self).text_primary),
            )
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
        previous_height = len(self._line_strips) + len(self._pending_strips)
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
        current_height = len(self._line_strips) + len(self._pending_strips)
        self.refresh(layout=rebuild or current_height != previous_height)

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
        base_style = Style(color=theme_for(self).text_primary)
        options = console.options.update_width(self._render_width)
        segments = console.render(_ReasoningContent(line, theme_for(self)), options)
        return [
            strip.adjust_cell_length(self._render_width, base_style).apply_style(base_style)
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
        base_style = Style(color=theme_for(self).text_primary)
        options = console.options.update_width(self._render_width)
        rendered_lines = console.render_lines(Text(line, style=base_style), options, pad=False)
        if not rendered_lines:
            return [Strip.blank(self._render_width, base_style)]
        return [
            Strip(segments)
            .adjust_cell_length(self._render_width, base_style)
            .apply_style(base_style)
            for segments in rendered_lines
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


@dataclass(frozen=True)
class _EntryRenderRequest:
    """Detached input for rendering one history entry outside the UI loop."""

    entry: TranscriptEntry
    previous_kind: TranscriptEntryKind | None
    entry_index: int
    theme: TuiTheme
    reasoning_expanded: bool
    tool_highlighted: bool = False
    reasoning_highlighted: bool = False


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
    reasoning_expanded: bool
    reasoning_expanded_by_entry: dict[int, bool]
    theme: TuiTheme


def _summary_line(
    title: str,
    *,
    marker_tone: str,
    details: Iterable[tuple[str, str]],
    theme: TuiTheme,
    emphasized: bool = True,
) -> Text:
    summary = Text()
    marker_style = f"bold {marker_tone}" if emphasized else marker_tone
    title_style = (
        f"bold {theme.text_secondary}" if emphasized else theme.text_secondary
    )
    summary.append(f"{theme.brand_mark} ", style=marker_style)
    summary.append(title, style=title_style)
    for value, tone in details:
        summary.append(theme.section_separator, style=theme.separator_tone)
        summary.append(value, style=tone)
    return summary


def _compaction_summary(compaction: CompactionPresentation, theme: TuiTheme) -> Text:
    noun = "message" if compaction.covered_message_count == 1 else "messages"
    details = [
        (
            f"{compaction.covered_message_count} {noun}",
            theme.text_metadata,
        )
    ]
    if compaction.tokens_reduced is not None:
        details.append(
            (
                f"{format_compact_count(compaction.tokens_reduced)} tokens",
                theme.text_metadata,
            )
        )
    if compaction.duration_seconds is not None:
        details.append(
            (
                format_duration(compaction.duration_seconds),
                theme.text_metadata,
            )
        )
    return _summary_line(
        "Context compacted",
        marker_tone=theme.accent,
        details=details,
        theme=theme,
    )


def _run_summary(run: RunPresentation, theme: TuiTheme) -> Text:
    marker_tone = (
        theme.failure
        if run.status == "failed"
        else theme.accent
        if run.status == "completed"
        else theme.text_muted
    )
    details: list[tuple[str, str]] = []
    if run.duration_seconds is not None:
        details.append((format_duration(run.duration_seconds), theme.text_metadata))
    if run.status != "completed":
        status_tone = theme.failure if run.status == "failed" else theme.text_metadata
        details.append((run.status, status_tone))
    if run.status == "failed" and run.error:
        details.append((" ".join(run.error.split()), theme.failure))
    return _summary_line(
        run.model,
        marker_tone=marker_tone,
        details=details,
        theme=theme,
        emphasized=False,
    )


def _reasoning_area(
    entry: TranscriptEntry,
    *,
    entry_index: int,
    expanded: bool,
    top: int,
    theme: TuiTheme,
    highlighted: bool = False,
) -> tuple[RenderableType, _PieceTag]:
    """The reasoning area of one entry: label plus the togglable body.

    The area always ends in exactly one blank line — the gap belongs between
    reasoning and its answer whether or not an answer follows.
    """
    label = _thought_label(
        entry.reasoning_seconds,
        entry_index=entry_index,
        expanded=expanded,
        theme=theme,
        highlighted=highlighted,
    )
    if expanded:
        area: RenderableType = Group(
            label,
            # Keep the border in the label's column. The trailing cell mirrors
            # ReasoningLines' right padding, so live and frozen thoughts retain
            # the same content width and wrapping when the stream is promoted.
            Padding(_ReasoningContent(entry.reasoning, theme), (1, 1, 0, 0)),
        )
    else:
        area = label
    return Padding(area, (top, 2, 1, 2)), "reasoning"


def _entry_renderables(
    request: _EntryRenderRequest,
    render_tool: ToolActivityRenderer,
) -> list[tuple[RenderableType, _PieceTag]]:
    """Build Rich renderables from a detached presentation snapshot."""

    entry = request.entry
    theme = request.theme
    if entry.kind == "user":
        return [
            (
                Padding(_UserMessageContent(entry.text, theme), (1, 2, 2, 2)),
                "user",
            )
        ]
    if entry.kind == "tool":
        assert entry.tool is not None
        rendered = render_tool(
            entry.tool,
            frame=0,
            theme=theme,
            agent=entry.agent,
        )
        if entry.tool.tool_name == "SpawnAgent" and entry.run_id is not None:
            interaction_style = Style(meta={_TOOL_NAVIGATION_META: request.entry_index})
            if request.tool_highlighted:
                interaction_style += Style(color=theme.text_highlight)
            rendered.stylize(interaction_style, 0, len(rendered))
        return [(Padding(rendered, (0, 2, 0, 2)), "body")]
    if entry.kind == "notice":
        tone = theme.critical if entry.error else theme.accent
        top = 1 if request.previous_kind == "tool" else 0
        return [(Padding(Text(entry.text, style=tone), (top, 1, 1, 1)), "body")]
    if entry.kind == "compaction":
        assert entry.compaction is not None
        top = 1 if request.previous_kind == "tool" else 0
        summary = _compaction_summary(entry.compaction, theme)
        return [(Padding(summary, (top, 1, 1, 2)), "body")]
    if entry.kind == "run":
        assert entry.run is not None
        summary = _run_summary(entry.run, theme)
        return [(Padding(summary, (0, 1, 1, 2)), "body")]

    pieces: list[tuple[RenderableType, _PieceTag]] = []
    top = 1 if request.previous_kind == "tool" else 0
    if entry.reasoning:
        pieces.append(
            _reasoning_area(
                entry,
                entry_index=request.entry_index,
                expanded=request.reasoning_expanded,
                top=top,
                theme=theme,
                highlighted=request.reasoning_highlighted,
            )
        )
    if entry.text:
        text_top = 0 if entry.reasoning else top
        pieces.append(
            (
                Padding(assistant_markdown(entry.text, theme), (text_top, 2, 1, 2)),
                "body",
            )
        )
    return pieces


def _render_strips_at_width(
    console: Console,
    options: ConsoleOptions,
    renderable: RenderableType,
) -> list[Strip]:
    """Render one immutable piece using captured console geometry."""

    segments = console.render(renderable, options)
    strips = [
        strip.adjust_cell_length(options.max_width)
        for strip in Strip.from_lines(list(Segment.split_lines(segments)))
    ]
    return strips or [Strip.blank(options.max_width)]


def _render_entry_requests(
    requests: tuple[_EntryRenderRequest, ...],
    *,
    render_tool: ToolActivityRenderer,
    console: Console,
    options: ConsoleOptions,
) -> tuple[_RenderedEntry, ...]:
    """Parse Markdown, highlight code, and render a detached entry batch."""

    render_contexts: dict[TuiTheme, tuple[Console, ConsoleOptions]] = {}

    def context_for(theme: TuiTheme) -> tuple[Console, ConsoleOptions]:
        try:
            return render_contexts[theme]
        except KeyError:
            render_console = Console(
                color_system=console.color_system,  # type: ignore[arg-type]
                force_terminal=console.is_terminal,
                theme=theme.rich_theme(),
                width=options.max_width,
                no_color=console.no_color,
                tab_size=console.tab_size,
                legacy_windows=console.legacy_windows,
                safe_box=console.safe_box,
            )
            context = (
                render_console,
                render_console.options.update_width(options.max_width),
            )
            render_contexts[theme] = context
            return context

    return tuple(
        _RenderedEntry(
            entry_index=request.entry_index,
            pieces=tuple(
                (
                    _render_strips_at_width(
                        *context_for(request.theme),
                        renderable,
                    ),
                    tag,
                )
                for renderable, tag in _entry_renderables(request, render_tool)
            ),
        )
        for request in requests
    )


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
        self._user_lines: list[int] = []
        self._render_width = 0
        self._reasoning_expanded = reasoning_expanded
        self._reasoning_expanded_by_entry: dict[int, bool] = {}
        self._hovered_tool_entry_index: int | None = None
        self._hovered_reasoning_entry_index: int | None = None
        self._theme_render_revision = 0
        self._theme_render_active = False
        self._theme_render_idle = asyncio.Event()
        self._theme_render_idle.set()

    @property
    def entries(self) -> tuple[TranscriptEntry, ...]:
        return tuple(self._entries)

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

    def on_mouse_move(self, event: events.MouseMove) -> None:
        hovered_tool = None
        hovered_reasoning = None
        if event.style is not None:
            tool_entry_index = event.style.meta.get(_TOOL_NAVIGATION_META)
            if isinstance(tool_entry_index, int):
                hovered_tool = tool_entry_index
            reasoning_entry_index = event.style.meta.get(_REASONING_ENTRY_META)
            if isinstance(reasoning_entry_index, int):
                hovered_reasoning = reasoning_entry_index
        self._set_hovered_entry("_hovered_tool_entry_index", hovered_tool)
        self._set_hovered_entry("_hovered_reasoning_entry_index", hovered_reasoning)

    def on_leave(self, _event: events.Leave) -> None:
        self._set_hovered_entry("_hovered_tool_entry_index", None)
        self._set_hovered_entry("_hovered_reasoning_entry_index", None)

    def _set_hovered_entry(self, attribute: str, entry_index: int | None) -> None:
        previous = getattr(self, attribute)
        if previous == entry_index:
            return
        setattr(self, attribute, entry_index)
        for changed in (previous, entry_index):
            if changed is not None and 0 <= changed < len(self._entries):
                self._rerender_entry(changed)

    def replace(self, entries: Iterable[TranscriptEntry]) -> None:
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
                self._hovered_tool_entry_index,
                self._hovered_reasoning_entry_index,
            )
            if index is not None
        }
        self._hovered_tool_entry_index = None
        self._hovered_reasoning_entry_index = None
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
            reasoning_expanded=self._reasoning_expanded,
            reasoning_expanded_by_entry=self._reasoning_expanded_by_entry,
            theme=theme_for(self),
        )
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
        self._line_cache.clear()
        self.virtual_size = Size(0, 0)
        self.display = False
        return state

    async def load(
        self,
        entries: Iterable[TranscriptEntry],
        *,
        reusable_state: ConversationHistoryState | None = None,
    ) -> None:
        """Replace the transcript, reusing rendered state where it is canonical.

        Unchanged entries retain their Rich strips. Mutable tool and Run
        projections re-render in place, while a structural change renders only
        the suffix after the shared prefix. A cold load remains chunked.
        """
        self._hovered_tool_entry_index = None
        self._hovered_reasoning_entry_index = None
        materialized = list(entries)
        if materialized and (
            not self._size_known or self.scrollable_content_region.width <= 0
        ):
            # Reveal an empty/reused log before attaching entries. Layout can
            # establish the render width without synchronously rendering content
            # from Resize, which keeps both cold loads and cached restores on
            # their intended paths.
            self.display = True
            for _attempt in range(4):
                await self.wait_for_refresh()
                if self._size_known and self.scrollable_content_region.width > 0:
                    break
        rendered = self._restore_state(reusable_state, materialized)
        if rendered is None:
            self._reasoning_expanded_by_entry.clear()
            if not self._reset_buffer(materialized):
                if not self._entries:
                    return
            rendered = 0
        elif rendered == len(self._entries):
            return
        if not self._size_known or self.scrollable_content_region.width <= 0:
            # Making a previously empty log visible schedules layout. Wait for
            # geometry before choosing the render path so a large first load does
            # not fall back to synchronous Markdown parsing on the UI loop.
            for _attempt in range(4):
                await self.wait_for_refresh()
                if self._size_known and self.scrollable_content_region.width > 0:
                    break
            else:
                self.call_after_refresh(self._render_entries)
                await self.wait_until_rendered()
                return
        try:
            while rendered < len(self._entries):
                next_rendered = await self._write_entries_async(
                    rendered,
                    min(rendered + _LOAD_CHUNK_ENTRIES, len(self._entries)),
                )
                await self.wait_for_refresh()
                rendered = next_rendered
        except asyncio.CancelledError:
            # Exclusive workers supersede an in-flight session load; finish the
            # remaining entries off-loop so the buffer never rests half-rendered
            # before the cancellation propagates.
            while rendered < len(self._entries):
                rendered = await self._write_entries_async(
                    rendered,
                    len(self._entries),
                    background=True,
                )
            raise
        self._render_width = self.scrollable_content_region.width

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
        for cached, current in zip(state.entries, entries, strict=False):
            if not self._same_entry_boundary(cached, current):
                break
            shared += 1

        self._entries = state.entries
        self._pieces = state.pieces
        self._piece_line_starts = state.piece_line_starts
        self._piece_indexes_by_entry = state.piece_indexes_by_entry
        self._user_lines = state.user_lines
        self.lines = state.lines
        self._widest_line_width = state.widest_line_width
        self._render_width = state.render_width
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
        self._tool_entry_indexes = {
            (entry.run_id, entry.tool.tool_call_id): index
            for index, entry in enumerate(self._entries)
            if entry.tool is not None
        }
        for index in changed_indexes:
            if not self._rerender_entry(index):
                return None
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

    async def _write_entries_async(
        self,
        start: int,
        stop: int,
        *,
        background: bool = False,
    ) -> int:
        """Render a history chunk, moving frame-sized Rich work off the UI loop."""

        entries = tuple(self._entries[start:stop])
        if not entries:
            return stop
        if not background and not self._requires_background_render(entries):
            return self._write_entries(start, stop)

        requests = self._render_requests(
            entries,
            start_index=start,
            previous_kind=self._entries[start - 1].kind if start else None,
        )
        console = self.app.console
        viewport_width = self.scrollable_content_region.width
        render_width = max(viewport_width, self.min_width)
        options = console.options.update_width(render_width)
        expected_piece_count = len(self._pieces)
        rendered = await asyncio.to_thread(
            _render_entry_requests,
            requests,
            render_tool=self._render_tool,
            console=console,
            options=options,
        )
        if len(self._pieces) != expected_piece_count:
            # A resize or theme refresh rebuilt the complete buffer while this
            # detached batch was rendering; that newer complete buffer wins.
            return len(self._entries)
        if theme_for(self) != requests[0].theme:
            # Theme preview changed while this detached chunk was rendering.
            # Retry with the latest immutable theme instead of committing stale strips.
            return start
        if self.scrollable_content_region.width != viewport_width:
            # Geometry changed before its Resize callback rebuilt history. Retry
            # this chunk after the next refresh instead of committing stale strips.
            return start
        self._append_rendered_entries(rendered)
        return stop

    def append(
        self,
        entry: TranscriptEntry,
        *,
        reasoning_expanded: bool | None = None,
    ) -> None:
        entry_index = self._register_entry(entry, reasoning_expanded=reasoning_expanded)
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

    def append_many(self, entries: Iterable[TranscriptEntry]) -> None:
        """Append one immutable batch and publish its history size once."""

        materialized = tuple(entries)
        if not materialized:
            return
        start = len(self._entries)
        for entry in materialized:
            self._register_entry(entry)
        self.display = True
        if self._size_known and self.scrollable_content_region.width > 0:
            self._write_entries(start, len(self._entries))
        else:
            self.call_after_refresh(self._render_entries)

    async def append_many_rendered(self, entries: Iterable[TranscriptEntry]) -> None:
        """Append Markdown entries while keeping expensive Rich work off-loop."""

        materialized = tuple(entries)
        if not materialized:
            return
        if (
            not self._size_known
            or self.scrollable_content_region.width <= 0
            or not self._requires_background_render(materialized)
        ):
            self.append_many(materialized)
            return

        while True:
            start = len(self._entries)
            previous_kind = self._entries[-1].kind if self._entries else None
            requests = self._render_requests(
                materialized,
                start_index=start,
                previous_kind=previous_kind,
            )
            console = self.app.console
            viewport_width = self.scrollable_content_region.width
            render_width = max(viewport_width, self.min_width)
            options = console.options.update_width(render_width)
            rendered = await asyncio.to_thread(
                _render_entry_requests,
                requests,
                render_tool=self._render_tool,
                console=console,
                options=options,
            )
            if len(self._entries) != start:
                # The message pump normally serializes transcript mutations. Keep
                # the buffer internally consistent if an external caller races this await.
                self.append_many(materialized)
                return
            if (
                self.scrollable_content_region.width == viewport_width
                and theme_for(self) == requests[0].theme
            ):
                for entry in materialized:
                    self._register_entry(entry)
                self.display = True
                self._append_rendered_entries(rendered)
                return
            await self.wait_for_refresh()
            if (
                not self._size_known
                or self.scrollable_content_region.width <= 0
                or not self._requires_background_render(materialized)
            ):
                self.append_many(materialized)
                return

    def _register_entry(
        self,
        entry: TranscriptEntry,
        *,
        reasoning_expanded: bool | None = None,
    ) -> int:
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
        return entry_index

    @staticmethod
    def _requires_background_render(entries: tuple[TranscriptEntry, ...]) -> bool:
        if len(entries) > 1:
            return True
        if any(entry.tool is not None for entry in entries):
            return True
        return sum(len(entry.text) for entry in entries) >= _BACKGROUND_MARKDOWN_CHARS

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
        self._theme_render_revision += 1
        if not self._entries or not self.is_mounted or self._theme_render_active:
            return
        self._theme_render_active = True
        self._theme_render_idle.clear()
        self.run_worker(
            self._refresh_theme_async(),
            group="conversation-history-theme",
        )

    async def wait_for_theme_refresh(self) -> None:
        """Wait until the latest coalesced theme render reaches the visible buffer."""

        await self._theme_render_idle.wait()

    async def _refresh_theme_async(self) -> None:
        """Render a complete themed buffer off-loop, then replace it atomically."""

        try:
            while self._entries:
                revision = self._theme_render_revision
                width = self.scrollable_content_region.width
                entries = tuple(self._entries)
                if width <= 0:
                    return
                requests = self._render_requests(
                    entries,
                    start_index=0,
                    previous_kind=None,
                )
                console = self.app.console
                render_width = max(width, self.min_width)
                options = console.options.update_width(render_width)
                rendered = await asyncio.to_thread(
                    _render_entry_requests,
                    requests,
                    render_tool=self._render_tool,
                    console=console,
                    options=options,
                )
                current_requests = self._render_requests(
                    tuple(self._entries),
                    start_index=0,
                    previous_kind=None,
                )
                if (
                    revision != self._theme_render_revision
                    or self.scrollable_content_region.width != width
                    or current_requests != requests
                ):
                    continue

                self._pieces.clear()
                self._piece_line_starts.clear()
                self._piece_indexes_by_entry.clear()
                self._user_lines.clear()
                self._widest_line_width = 0
                self._line_cache.clear()
                self.lines.clear()
                self._append_rendered_entries(rendered)
                self._render_width = width
                self.refresh(layout=True)
                return
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
            theme=theme_for(self),
            highlighted=self._hovered_reasoning_entry_index == entry_index,
        )
        return self._render_strips(renderable)

    def on_resize(self, event: Resize) -> None:
        size_was_known = self._size_known
        super().on_resize(event)
        width = self.scrollable_content_region.width
        if self._entries and width > 0 and (
            not self.lines or not size_was_known or width != self._render_width
        ):
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
        self._user_lines = []
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
        for strips, tag in rendered.pieces:
            if tag == "user":
                self._user_lines.append(len(self.lines))
            self._piece_line_starts.append(len(self.lines))
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
                    tool_highlighted=self._hovered_tool_entry_index == entry_index,
                    reasoning_highlighted=(self._hovered_reasoning_entry_index == entry_index),
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

        self.virtual_size = Size(self._widest_line_width, len(self.lines))

    def _rebuild_lines(self) -> None:
        """Rebuild the visible line space from pieces; strips are reused as-is."""
        self._line_cache.clear()
        lines: list[Strip] = []
        starts: list[int] = []
        user_lines: list[int] = []
        piece_indexes_by_entry: dict[int, list[int]] = {}
        widest = 0
        for piece_index, piece in enumerate(self._pieces):
            starts.append(len(lines))
            piece_indexes_by_entry.setdefault(piece.entry_index, []).append(piece_index)
            if piece.tag == "user":
                user_lines.append(len(lines))
            widest = max(widest, piece.strips[0].cell_length)
            lines.extend(piece.strips)
        self.lines = lines
        self._piece_line_starts = starts
        self._piece_indexes_by_entry = piece_indexes_by_entry
        self._tool_entry_indexes = {
            (entry.run_id, entry.tool.tool_call_id): index
            for index, entry in enumerate(self._entries)
            if entry.tool is not None
        }
        self._user_lines = user_lines
        self._widest_line_width = widest
        self.virtual_size = Size(widest, len(lines))
        self.refresh()
