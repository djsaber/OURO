"""Terminal status line presentation."""

from __future__ import annotations

from math import ceil

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static

from ouro.application.contracts.agents import AgentTreeNode
from ouro.application.contracts.sessions import SessionContextStatus
from ouro.core.context import ContextMetrics
from ouro.core.context_budget import ContextBudget
from ouro.tui.state import format_compact_count
from ouro.tui.theme import theme_for
from ouro.tui.view_models import TuiApprovalPreset
from ouro.tui.widgets.activity import ActivityIndicator

_CONTEXT_PROGRESS_WIDTH = 11


class StatusLine(Horizontal):
    def __init__(self) -> None:
        super().__init__()
        self._request: SessionContextStatus | None = None
        self._history_epoch = 0
        self._cache_hit_ratio: float | None = None
        self._context_expanded = False
        self._context_metrics: ContextMetrics | None = None
        self._context_width = 0
        self._context_budget: ContextBudget | None = None

    def compose(self) -> ComposeResult:
        yield ActivityIndicator()
        yield Static("", id="context-detail")

    def set_activity(
        self,
        activity: str,
        *,
        model_attempt: int | None = None,
        model_max_attempts: int | None = None,
    ) -> None:
        self.query_one(ActivityIndicator).set_activity(
            activity,
            model_attempt=model_attempt,
            model_max_attempts=model_max_attempts,
        )

    def set_model(self, model: str) -> None:
        self.query_one(ActivityIndicator).set_model(model)

    def set_reasoning(self, value: str | None, *, peak: bool = False) -> None:
        self.query_one(ActivityIndicator).set_reasoning(value, peak=peak)

    def set_approval_mode(self, mode: TuiApprovalPreset) -> None:
        self.query_one(ActivityIndicator).set_approval_mode(mode)

    def set_session_controls(self, labels: tuple[str, ...]) -> None:
        self.query_one(ActivityIndicator).set_session_controls(labels)

    def set_subagent(self, agent: AgentTreeNode | None) -> None:
        self.query_one(ActivityIndicator).set_subagent(agent)

    def set_context(
        self,
        request: SessionContextStatus | None,
        *,
        effective_metrics: ContextMetrics | None = None,
        history_epoch: int = 0,
    ) -> None:
        metrics = effective_metrics or (request.metrics if request is not None else None)
        previous = self._request
        if (
            request == previous
            and metrics == self._context_metrics
            and history_epoch == self._history_epoch
        ):
            return
        if (
            metrics is None
            or self._history_epoch != history_epoch
            or (
                previous is not None
                and (request is None or previous.session_id != request.session_id)
            )
        ):
            self._cache_hit_ratio = None
            self._context_expanded = False
        self._history_epoch = history_epoch
        if request is not None:
            total = request.provider_reported_input_tokens
            cached = request.provider_reported_cache_read_tokens
            if total is not None and total > 0 and cached is not None:
                self._cache_hit_ratio = cached / total
        self._request = request
        self._context_metrics = metrics
        self._render_context()

    @on(events.Click, "#context-detail")
    def _toggle_context(self, event: events.Click) -> None:
        if event.button == 1:
            self._context_expanded = not self._context_expanded
            self._render_context()
            event.stop()

    @on(events.Enter, "#context-detail")
    @on(events.Leave, "#context-detail")
    def _context_hover_changed(self) -> None:
        self._render_context()

    def set_context_budget(self, budget: ContextBudget | None) -> None:
        if budget == self._context_budget:
            return
        self._context_budget = budget
        self._render_context()

    def refresh_theme(self) -> None:
        self._render_context()

    def _render_context(self) -> None:
        detail = self.query_one("#context-detail", Static)
        metrics = self._context_metrics
        if metrics is not None and self._context_budget is not None:
            metrics = metrics.with_budget(self._context_budget)
        theme = theme_for(self)
        content = Text()
        if metrics is not None:
            marker = (
                theme.expanded_disclosure_marker
                if self._context_expanded
                else theme.collapsed_disclosure_marker
            )
            content.append(
                marker, style=theme.text_highlight if detail.mouse_hover else theme.text_lowlight
            )
            used = metrics.estimated_input_tokens
            content.append(f" {format_compact_count(used)}")
            request = self._request
            if (
                self._context_expanded
                and request is not None
                and request.provider_reported_input_tokens != 0
            ):
                rate = self._cache_hit_ratio
                if rate is not None:
                    content.append(f"{theme.inline_separator}{rate:.1%} cached")
                elif request.completed_at is None and request.interrupted_at is None:
                    content.append(f"{theme.inline_separator}— cached")
            budget = metrics.input_budget_tokens
            if budget is not None:
                ratio = used / budget
                bounded_ratio = min(ratio, 1.0)
                completed = ceil(bounded_ratio * _CONTEXT_PROGRESS_WIDTH)
                content.append(" ")
                content.append(theme.progress_filled_marker * completed, style=theme.accent)
                content.append(
                    theme.progress_empty_marker * (_CONTEXT_PROGRESS_WIDTH - completed),
                    style=theme.progress_track,
                )
                percentage_tone = theme.warning if ratio > 0.8 else theme.text_lowlight
                content.append(f" {ratio:.0%}", style=percentage_tone)
        content_width = content.cell_len
        detail.update(content, layout=content_width != self._context_width)
        self._context_width = content_width
