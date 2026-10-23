"""Terminal status line presentation."""

from __future__ import annotations

from math import ceil

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static

from ouro.application.contracts.agents import AgentTreeNode
from ouro.application.contracts.sessions import SessionContextStatus
from ouro.core.context import ContextMetrics
from ouro.tui.state import format_compact_count
from ouro.tui.theme import theme_for
from ouro.tui.view_models import TuiApprovalPreset
from ouro.tui.widgets.activity import ActivityIndicator

_CONTEXT_PROGRESS_WIDTH = 11


class StatusLine(Horizontal):
    def __init__(self) -> None:
        super().__init__()
        self._context_metrics: ContextMetrics | None = None
        self._context_width = 0

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

    def set_context(self, status: SessionContextStatus | None) -> None:
        self.set_context_metrics(status.metrics if status is not None else None)

    def set_context_metrics(self, metrics: ContextMetrics | None) -> None:
        if metrics == self._context_metrics:
            return
        self._context_metrics = metrics
        self._render_context()

    def refresh_theme(self) -> None:
        self._render_context()

    def _render_context(self) -> None:
        metrics = self._context_metrics
        theme = theme_for(self)
        if metrics is None:
            content = Text()
        else:
            used = metrics.estimated_input_tokens
            budget = metrics.input_budget_tokens
            if budget is None:
                content = Text(format_compact_count(used))
            else:
                ratio = used / budget
                bounded_ratio = min(ratio, 1.0)
                completed = ceil(bounded_ratio * _CONTEXT_PROGRESS_WIDTH)
                content = Text(f"{format_compact_count(used)} ")
                content.append(theme.progress_filled_marker * completed, style=theme.accent)
                content.append(
                    theme.progress_empty_marker * (_CONTEXT_PROGRESS_WIDTH - completed),
                    style=theme.progress_track,
                )
                percentage_tone = theme.warning if ratio > 0.8 else theme.text_lowlight
                content.append(f" {ratio:.0%}", style=percentage_tone)
        content_width = content.cell_len
        self.query_one("#context-detail", Static).update(
            content,
            layout=content_width != self._context_width,
        )
        self._context_width = content_width
