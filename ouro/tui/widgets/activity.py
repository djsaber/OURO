"""Run activity indicator presentation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from rich.text import Text
from textual.timer import Timer
from textual.widgets import Static

from ouro.application.contracts.agents import AgentTreeNode
from ouro.tui.status import _ACTIVITY_LABELS, _subagent_task_status
from ouro.tui.theme import theme_for
from ouro.tui.view_models import TuiApprovalPreset

_BUSY_ACTIVITIES = {
    "starting",
    "loading_session",
    "mcp_connecting",
    "queued",
    "preparing_model",
    "waiting_model",
    "streaming_reasoning",
    "streaming_text",
    "streaming_tool_call",
    "preparing_tools",
    "executing_tools",
    "committing_results",
    "finalizing_response",
    "compacting",
    "forking",
    "cancelling",
}


class ActivityIndicator(Static):
    """Animated activity and model status shown beside the composer."""

    def __init__(self) -> None:
        super().__init__(id="activity-indicator")
        self._frame = 0
        self._activity = "idle"
        self._pending_operations = 0
        self._show_pending = False
        self._approval_mode: TuiApprovalPreset = "inherit"
        self._model = "starting"
        self._model_attempt: int | None = None
        self._model_max_attempts: int | None = None
        self._reasoning: str | None = None
        self._reasoning_peak = False
        self._subagent: AgentTreeNode | None = None
        self._session_control_labels: tuple[str, ...] = ()
        self._timer: Timer | None = None

    def on_mount(self) -> None:
        self._sync_spinner_timer(force=True)
        self._render_indicator()

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def set_activity(
        self,
        activity: str,
        *,
        model_attempt: int | None = None,
        model_max_attempts: int | None = None,
    ) -> None:
        if (
            activity == self._activity
            and model_attempt == self._model_attempt
            and model_max_attempts == self._model_max_attempts
        ):
            return
        self._activity = activity
        self._model_attempt = model_attempt
        self._model_max_attempts = model_max_attempts
        self._sync_spinner_timer()
        if activity not in _BUSY_ACTIVITIES:
            self._frame = 0
        self._render_indicator()

    def set_model(self, model: str) -> None:
        if model == self._model:
            return
        self._model = model
        self._render_indicator()

    @contextmanager
    def pending_operation(self) -> Iterator[None]:
        """Show menu work without replacing the session's runtime activity."""

        self._pending_operations += 1
        self.call_later(self._refresh_pending)
        try:
            yield
        finally:
            self._pending_operations -= 1
            self._refresh_pending()

    def _refresh_pending(self) -> None:
        pending = self._pending_operations > 0
        if not self.is_attached or self._show_pending == pending:
            return
        self._show_pending = pending
        self._sync_spinner_timer()
        self._render_indicator()

    def set_approval_mode(self, mode: TuiApprovalPreset) -> None:
        if mode == self._approval_mode:
            return
        self._approval_mode = mode
        self._render_indicator()

    def set_reasoning(self, value: str | None, *, peak: bool = False) -> None:
        """Show the effective thinking level beside the model.

        ``peak`` highlights the level when it is the highest effort the current
        endpoint supports, signalling that reasoning is maxed out.
        """
        if value == self._reasoning and peak == self._reasoning_peak:
            return
        self._reasoning = value
        self._reasoning_peak = peak
        self._render_indicator()

    def set_session_controls(self, labels: tuple[str, ...]) -> None:
        unique_labels = tuple(dict.fromkeys(labels))
        if unique_labels == self._session_control_labels:
            return
        self._session_control_labels = unique_labels
        self._render_indicator()

    def set_subagent(self, agent: AgentTreeNode | None) -> None:
        if agent == self._subagent:
            return
        self._subagent = agent
        self._render_indicator()

    def refresh_theme(self) -> None:
        self._frame = 0
        self._sync_spinner_timer(force=True)
        self._render_indicator()

    def _tick(self) -> None:
        theme = theme_for(self)
        self._frame = (self._frame + 1) % len(theme.spinner_frames)
        self._render_indicator()

    def _sync_spinner_timer(self, *, force: bool = False) -> None:
        if not self.is_attached:
            return
        if force and self._timer is not None:
            self._timer.stop()
            self._timer = None
        theme = theme_for(self)
        animated = len(theme.spinner_frames) > 1
        busy = self._activity in _BUSY_ACTIVITIES or self._show_pending
        if animated and self._timer is None:
            self._timer = self.set_interval(
                theme.spinner_interval_seconds,
                self._tick,
                pause=not busy,
            )
        elif self._timer is not None:
            if busy:
                self._timer.resume()
            else:
                self._timer.pause()

    def _render_indicator(self) -> None:
        theme = theme_for(self)
        accent = theme.accent
        busy = self._activity in _BUSY_ACTIVITIES or self._show_pending
        ready = self._activity in {"idle", "completed"}
        failed = self._activity == "failed"
        mark = theme.spinner_frames[self._frame] if busy else theme.brand_mark
        label = _ACTIVITY_LABELS.get(self._activity, self._activity.replace("_", " ").title())
        if self._show_pending and ready:
            label = "Working"
        elif self._subagent is not None and ready:
            subagent_status = _subagent_task_status(self._subagent)
            label = subagent_status.title()
            ready = subagent_status == "done"
            failed = subagent_status == "failed"
        if (
            self._activity == "waiting_model"
            and self._model_attempt is not None
            and self._model_max_attempts is not None
            and 2 <= self._model_attempt <= self._model_max_attempts
        ):
            label = f"{label} {self._model_attempt}/{self._model_max_attempts}"
        if busy:
            marker_style = f"bold {accent}"
        elif failed:
            marker_style = f"bold {theme.failure}"
        elif ready:
            marker_style = f"bold {accent}"
        else:
            marker_style = theme.text_primary
        content = Text()
        content.append(f"{mark} ", style=marker_style)
        for control_label in self._session_control_labels:
            content.append(control_label, style=f"bold {accent}")
            content.append(
                theme.section_separator,
                style=theme.text_lowlight,
            )
        content.append(label, style=theme.text_primary)
        content.append(
            theme.section_separator,
            style=theme.text_lowlight,
        )
        approval_style = (
            theme.danger if self._approval_mode in {"auto", "afk"} else theme.text_primary
        )
        content.append(self._approval_mode, style=approval_style)
        if self._model:
            content.append(
                theme.section_separator,
                style=theme.text_lowlight,
            )
            content.append(self._model, style=theme.text_primary)
        if self._reasoning:
            style = f"bold {accent}" if self._reasoning_peak else theme.text_primary
            content.append(
                theme.section_separator,
                style=theme.text_lowlight,
            )
            content.append(self._reasoning, style=style)
        # This row is constrained to the single-line remainder of StatusLine;
        # spinner and status changes can only alter its paint, not its geometry.
        self.update(content, layout=False)
