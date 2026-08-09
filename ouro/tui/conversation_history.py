from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, Protocol

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.markdown import Markdown as RichMarkdown
from rich.padding import Padding
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.events import Resize
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import RichLog

from ouro.application.supervisor_models import RuntimeToolCallActivity
from ouro.tui.theme import TuiTheme, theme_for


class ToolActivityRenderer(Protocol):
    def __call__(
        self,
        tool: RuntimeToolCallActivity,
        *,
        frame: int,
        theme: TuiTheme,
    ) -> Text: ...


@dataclass(frozen=True)
class TranscriptEntry:
    """Immutable presentation input for one persisted transcript boundary."""

    kind: Literal["user", "assistant", "tool", "notice"]
    text: str = ""
    reasoning: str = ""
    tool: RuntimeToolCallActivity | None = None
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
        panel_style = Style(bgcolor="#171717")
        content = Text(self.text, style="#e5e7eb")
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
        content = Text(self.text, style="dim italic #8f9aa8")
        lines = console.render_lines(content, content_options, pad=False)
        border = Segment("│", Style(color="#505967"))
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


class ConversationHistory(RichLog, can_focus=False):
    """Line-buffered rendering for immutable persisted conversation history.

    A Textual widget owns a message pump even when it is off-screen. Persisted
    turns therefore belong in one line-oriented widget rather than a permanent
    DOM subtree per message and per Markdown block. Live output remains in
    regular widgets below this buffer because it is still mutable.
    """

    def __init__(self, render_tool: ToolActivityRenderer) -> None:
        super().__init__(
            id="conversation-history",
            wrap=True,
            min_width=1,
            auto_scroll=False,
        )
        self.display = False
        self._render_tool = render_tool
        self._entries: list[TranscriptEntry] = []
        self._user_lines: tuple[int, ...] = ()
        self._render_width = 0

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
        if event.button == 1 and event.style is not None and event.style.link:
            self.app.open_url(event.style.link)

    def replace(self, entries: Iterable[TranscriptEntry]) -> None:
        self._entries = list(entries)
        self._user_lines = ()
        self._render_width = 0
        super().clear()
        self.display = bool(self._entries)
        if self.display and self.scrollable_content_region.width > 0:
            self._render_entries()
        elif self.display:
            self.call_after_refresh(self._render_entries)

    def append(self, entry: TranscriptEntry) -> None:
        self._entries.append(entry)
        self.display = True
        if self._size_known and self.scrollable_content_region.width > 0:
            if entry.kind == "user":
                self._user_lines = (*self._user_lines, len(self.lines))
            super().write(
                self._render_entry(entry),
                expand=True,
                shrink=True,
                scroll_end=False,
            )
        else:
            self.call_after_refresh(self._render_entries)

    async def wait_until_rendered(self) -> None:
        """Wait for the initial width and line-buffer layout to become available."""

        for _ in range(3):
            if not self._entries or self.lines:
                return
            await self.wait_for_refresh()

    def update_tools(
        self,
        tools: Iterable[RuntimeToolCallActivity],
    ) -> frozenset[str]:
        updates = {tool.tool_call_id: tool for tool in tools}
        if not updates:
            return frozenset()
        matched: set[str] = set()
        changed = False
        for index, entry in enumerate(self._entries):
            if entry.tool is None:
                continue
            tool = updates.get(entry.tool.tool_call_id)
            if tool is None:
                continue
            matched.add(tool.tool_call_id)
            if entry.tool != tool:
                self._entries[index] = TranscriptEntry(kind="tool", tool=tool)
                changed = True
        if changed:
            self._render_entries()
        return frozenset(matched)

    def refresh_theme(self) -> None:
        if self._entries:
            self._render_entries()

    def on_resize(self, event: Resize) -> None:
        size_was_known = self._size_known
        super().on_resize(event)
        width = self.scrollable_content_region.width
        if self._entries and width > 0 and (not size_was_known or width != self._render_width):
            self._render_entries()

    def _render_entries(self) -> None:
        width = self.scrollable_content_region.width
        if width <= 0 or not self._size_known:
            return
        super().clear()
        user_lines: list[int] = []
        for entry in self._entries:
            if entry.kind == "user":
                user_lines.append(len(self.lines))
            super().write(
                self._render_entry(entry),
                expand=True,
                shrink=True,
                scroll_end=False,
            )
        self._user_lines = tuple(user_lines)
        self._render_width = width

    def _render_entry(self, entry: TranscriptEntry) -> RenderableType:
        theme = theme_for(self)
        if entry.kind == "user":
            return Padding(
                _UserMessageContent(entry.text, theme.accent),
                (1, 1, 2, 1),
            )
        if entry.kind == "tool":
            assert entry.tool is not None
            return Padding(
                self._render_tool(entry.tool, frame=0, theme=theme),
                (0, 0, 1, 2),
            )
        if entry.kind == "notice":
            tone = "#e08a8a" if entry.error else theme.accent
            return Padding(Text(entry.text, style=tone), (0, 1, 1, 1))

        blocks: list[RenderableType] = []
        if entry.reasoning:
            thought = Group(
                Text("THOUGHT", style="bold #788492"),
                Padding(
                    _ReasoningContent(entry.reasoning),
                    (0, 0, 1, 1),
                ),
            )
            blocks.append(thought)
        if entry.text:
            blocks.append(RichMarkdown(entry.text))
        return Padding(Group(*blocks), (0, 2, 1, 2))
