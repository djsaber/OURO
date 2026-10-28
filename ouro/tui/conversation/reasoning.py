"""Reasoning content presentation and collapse controls."""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.timer import Timer
from textual.widgets import Label

from ouro.tui.conversation.history_models import ReasoningLayout
from ouro.tui.conversation.history_rendering import ReasoningLines, _reasoning_label_text
from ouro.tui.selection import RangeSelectionOnly
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, theme_for


class ReasoningLabel(RangeSelectionOnly, Label):
    class ToggleRequested(Message):
        pass

    def __init__(self, content: Text, *, classes: str) -> None:
        self._content = content
        self._hovered = False
        super().__init__(content, classes=classes)

    def set_content(self, content: Text) -> None:
        layout = content.cell_len != self._content.cell_len
        self._content = content
        self._update_displayed_content(layout=layout)

    def on_enter(self, _event: events.Enter) -> None:
        self._hovered = True
        self._update_displayed_content()

    def on_leave(self, _event: events.Leave) -> None:
        self._hovered = False
        self._update_displayed_content()

    def _update_displayed_content(self, *, layout: bool = False) -> None:
        content = self._content.copy()
        if self._hovered:
            content.stylize(theme_for(self).text_highlight)
        self.update(content, layout=layout)

    def on_click(self, event: events.Click) -> None:
        if event.button == 1:
            self.post_message(self.ToggleRequested())
            event.stop()


class ReasoningView(RangeSelectionOnly, Vertical):
    """A plain-text reasoning stream with presentation state independent from the answer.

    Thinking output is shown verbatim rather than parsed as Markdown, so code fences and
    markup inside reasoning are never reformatted into blocks or syntax highlighting.
    Completed lines render once into the strip buffer; only the trailing partial line
    re-renders while thinking continues.
    """

    def __init__(self, *, expanded: bool = True) -> None:
        super().__init__(classes="reasoning-view")
        self.display = False
        self.label = ReasoningLabel(
            _completed_reasoning_label(None, expanded=expanded),
            classes="reasoning-label",
        )
        self._timer: Timer | None = None
        self._elapsed_at_start = 0.0
        self._clock_started_at: float | None = None
        self._completed_seconds: float | None = None
        self._content = ReasoningLines()
        self._content.set_class(not expanded, "collapsed")

    def on_mount(self) -> None:
        self._render_label()

    def refresh_theme(self) -> None:
        self._render_label()

    def compose(self) -> ComposeResult:
        yield self.label
        yield self._content

    def on_unmount(self) -> None:
        self._stop_timer()

    @property
    def text(self) -> str:
        return self._content.text

    async def completed_layout(self) -> ReasoningLayout | None:
        return await self._content.completed_layout()

    @property
    def duration_seconds(self) -> float | None:
        return self._completed_seconds

    @property
    def expanded(self) -> bool:
        return not self._content.has_class("collapsed")

    def set_expanded(self, expanded: bool) -> None:
        if expanded == self.expanded:
            return
        self._content.set_class(not expanded, "collapsed")
        self._render_label()

    def reset(self, *, expanded: bool) -> None:
        self._stop_timer()
        self._elapsed_at_start = 0.0
        self._completed_seconds = None
        self._content.replace("")
        self._content.set_class(not expanded, "collapsed")
        self.display = False
        self._render_label()

    async def append(self, text: str, *, started_at: datetime | None = None) -> None:
        if not text:
            return
        self.display = True
        started = self._clock_started_at is None
        if self._clock_started_at is None:
            now = datetime.now(UTC)
            self._elapsed_at_start = (
                max(0.0, (now - started_at).total_seconds()) if started_at is not None else 0.0
            )
            self._clock_started_at = monotonic()
            self._timer = self.set_interval(0.1, self._tick)
        if started:
            self._render_live_label()
        self._content.append(text)

    def finish(self, duration_seconds: float | None = None) -> None:
        if self.display:
            if duration_seconds is not None:
                elapsed = duration_seconds
            elif self._clock_started_at is None and self._completed_seconds is not None:
                elapsed = self._completed_seconds
            else:
                elapsed = self._elapsed_seconds()
            self._completed_seconds = elapsed
            self.label.set_content(
                _completed_reasoning_label(
                    elapsed,
                    expanded=self.expanded,
                    theme=theme_for(self),
                )
            )
        self._stop_timer()

    def _tick(self) -> None:
        self._render_live_label()

    def _render_live_label(self) -> None:
        self.label.set_content(
            _reasoning_label_text(
                "Thinking",
                self._elapsed_seconds(),
                expanded=self.expanded,
                theme=theme_for(self),
            )
        )

    def _render_label(self) -> None:
        if self._clock_started_at is not None:
            self._render_live_label()
            return
        self.label.set_content(
            _completed_reasoning_label(
                self._completed_seconds,
                expanded=self.expanded,
                theme=theme_for(self),
            )
        )

    def _elapsed_seconds(self) -> float:
        if self._clock_started_at is None:
            return self._elapsed_at_start
        return self._elapsed_at_start + monotonic() - self._clock_started_at

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._clock_started_at = None


def _completed_reasoning_label(
    duration_seconds: float | None,
    *,
    expanded: bool,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> Text:
    return _reasoning_label_text(
        "Thought",
        duration_seconds,
        expanded=expanded,
        theme=theme,
    )
