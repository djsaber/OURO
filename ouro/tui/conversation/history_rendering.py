"""History line rendering and entry layout helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.padding import Padding
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.events import Resize
from textual.geometry import Size
from textual.selection import Selection
from textual.strip import Strip
from textual.widget import Widget

from ouro.tui.conversation.history_models import (
    CompactionPresentation,
    ToolActivityRenderer,
    TranscriptEntry,
    _EntryRenderRequest,
    _PieceTag,
    _RenderedEntry,
)
from ouro.tui.conversation.messages import (
    _MESSAGE_META,
    _REASONING_ENTRY_META,
    _TOOL_NAVIGATION_META,
    _TRACE_ENTRY_META,
)
from ouro.tui.markdown import assistant_markdown
from ouro.tui.selection import RangeSelectionOnly
from ouro.tui.state import (
    RunPresentation,
    format_compact_count,
    format_duration,
    format_reasoning_label_parts,
)
from ouro.tui.text_wrapping import ProseText
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, theme_for


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
    text = Text(title, style=Style(color=theme.text_lowlight))
    if duration is not None:
        text.append(
            theme.inline_separator,
            style=Style(color=theme.text_lowlight),
        )
        text.append(duration, style=Style(color=theme.text_lowlight))
    return text


@dataclass(frozen=True)
class _MessageContent:
    """Share a literal text frame across user, scaffold and command messages."""

    text: str
    theme: TuiTheme
    created_at: datetime | None = None
    entry_index: int = -1
    highlighted: bool = False
    lowlight: bool = False

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
        interaction_meta = {_MESSAGE_META: self.entry_index}
        interaction_style = Style(meta=interaction_meta)
        panel_style = Style(
            bgcolor=self.theme.highlight if self.highlighted else self.theme.panel,
            meta=interaction_meta,
        )
        content = ProseText(
            self.text,
            style=self.theme.text_lowlight if self.lowlight else self.theme.text_emphasis,
        )
        lines = console.render_lines(content, content_options, pad=False)
        if self.created_at is not None:
            timestamp = Text(
                _format_user_message_timestamp(
                    self.created_at,
                    separator=self.theme.inline_separator,
                ),
                style=self.theme.text_lowlight,
            )
            lines.extend(console.render_lines(timestamp, content_options, pad=False))
        border = Segment(
            "│",
            Style(color=self.theme.text_lowlight if self.lowlight else self.theme.accent)
            + interaction_style,
        )

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


def _format_user_message_timestamp(
    created_at: datetime,
    *,
    now: datetime | None = None,
    separator: str = DEFAULT_TUI_THEME.inline_separator,
) -> str:
    """Format a message instant in the viewer's local time zone."""

    local_now = now or datetime.now().astimezone()
    local_created_at = created_at.astimezone(local_now.tzinfo)
    time_label = f"{local_created_at.hour:02d}:{local_created_at.minute:02d}"
    if local_created_at.date() == local_now.date():
        return time_label
    date_label = f"{local_created_at.year}/{local_created_at.month}/{local_created_at.day}"
    return f"{date_label}{separator}{time_label}"


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
        content = ProseText(self.text, style=f"italic {self.theme.text_lowlight}")
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

    # Match rendered_text's selection boundary, excluding trailing layout whitespace.
    text_length = len(strip.text.rstrip())
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
        lines = ProseText(source).wrap(
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
        base_style = Style(color=theme_for(self).text_emphasis)
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


def _summary_line(
    title: str,
    *,
    marker_tone: str,
    details: Iterable[tuple[str, str]],
    theme: TuiTheme,
) -> Text:
    summary = Text()
    summary.append(f"{theme.brand_mark} ", style=marker_tone)
    summary.append(title, style=theme.text_primary)
    for value, tone in details:
        summary.append(theme.section_separator, style=theme.text_lowlight)
        summary.append(value, style=tone)
    return summary


def _compaction_summary(compaction: CompactionPresentation, theme: TuiTheme) -> Text:
    noun = "message" if compaction.covered_message_count == 1 else "messages"
    details = [
        (
            f"{compaction.covered_message_count} {noun}",
            theme.text_lowlight,
        )
    ]
    if compaction.tokens_reduced is not None:
        details.append(
            (
                f"{format_compact_count(compaction.tokens_reduced)} tokens",
                theme.text_lowlight,
            )
        )
    if compaction.duration_seconds is not None:
        details.append(
            (
                format_duration(compaction.duration_seconds),
                theme.text_lowlight,
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
        else theme.text_primary
    )
    details: list[tuple[str, str]] = []
    if run.duration_seconds is not None:
        details.append((format_duration(run.duration_seconds), theme.text_lowlight))
    if run.status not in {"completed", "failed"}:
        details.append((run.status, theme.text_lowlight))
    if run.status == "failed" and run.error:
        details.append((" ".join(run.error.split()), theme.failure))
    return _summary_line(
        run.model,
        marker_tone=marker_tone,
        details=details,
        theme=theme,
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
    return Padding(area, (top, 0, 1, 0)), "reasoning"


def _entry_renderables(
    request: _EntryRenderRequest,
    render_tool: ToolActivityRenderer,
) -> list[tuple[RenderableType, _PieceTag]]:
    """Build Rich renderables from a detached presentation snapshot."""

    entry = request.entry
    theme = request.theme
    if entry.kind in {"user", "scaffold", "command"}:
        return [
            (
                Padding(
                    _MessageContent(
                        entry.text,
                        theme,
                        created_at=entry.created_at,
                        entry_index=request.entry_index,
                        highlighted=request.message_highlighted,
                        lowlight=entry.scaffold or entry.kind in {"scaffold", "command"},
                    ),
                    (1, 0, 2, 0),
                ),
                "user" if entry.kind == "user" else "body",
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
                rendered = theme.highlight_text(rendered)
            rendered.stylize(interaction_style, 0, len(rendered))
        return [(rendered, "body")]
    if entry.kind == "notice":
        top = 1 if request.previous_kind == "tool" else 0
        summary = _summary_line(entry.text, marker_tone=theme.accent, details=(), theme=theme)
        if entry.error:
            summary.stylize(theme.failure)
        return [(Padding(summary, (top, 0, 1, 0)), "body")]
    if entry.kind == "compaction":
        assert entry.compaction is not None
        top = 1 if request.previous_kind == "tool" else 0
        summary = _compaction_summary(entry.compaction, theme)
        return [(Padding(summary, (top, 0, 1, 0)), "body")]
    if entry.kind == "run":
        assert entry.run is not None
        summary = _run_summary(entry.run, theme)
        if request.trace_collapsed is not None:
            if request.trace_highlighted:
                marker = (
                    theme.collapsed_disclosure_marker
                    if request.trace_collapsed
                    else theme.expanded_disclosure_marker
                )
                summary = Text(marker) + summary[1:]
                summary.append(theme.section_separator, style=theme.text_lowlight)
                summary.append(f"{'show' if request.trace_collapsed else 'hide'} trace")
                summary = theme.highlight_text(summary)
            # Hover never changes the row height, even in a narrow terminal.
            summary.stylize(Style(meta={_TRACE_ENTRY_META: request.entry_index}))
            summary.no_wrap = True
            summary.overflow = "ellipsis"
        return [(Padding(summary, (0, 0, 1, 0)), "body")]

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
                Padding(assistant_markdown(entry.text, theme), (text_top, 0, 1, 0)),
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
