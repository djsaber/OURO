"""Reusable system, usage and keyboard information for welcome page packages."""

from __future__ import annotations

from rich.style import Style as RichStyle
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import Static

from ouro import __version__
from ouro.tui.state import format_compact_count
from ouro.tui.theme import TuiTheme, label_text, theme_for
from ouro.tui.view_models import TuiSystemStatus


def _compact_names(names: tuple[str, ...], *, limit: int = 3) -> str:
    visible = names[:limit]
    summary = ", ".join(visible)
    remaining = len(names) - len(visible)
    if remaining:
        summary += f", +{remaining}"
    return summary


class WelcomeInformation(Vertical):
    """Shared information typography, scoped to reusable information components."""

    DEFAULT_CSS = """
    WelcomeInformation {
        height: auto;
        background: transparent;
    }

    WelcomeInformation .welcome-title {
        width: auto;
        height: 1;
        color: $ouro-text-emphasis;
        text-style: bold;
    }

    WelcomeInformation .welcome-row {
        width: 100%;
        height: 1;
        overflow: hidden;
    }

    WelcomeInformation .welcome-label {
        width: 11;
        height: 1;
        color: $ouro-text-primary;
    }

    WelcomeInformation .welcome-value {
        width: 1fr;
        height: 1;
        overflow: hidden;
        text-overflow: ellipsis;
    }

    WelcomeInformation .welcome-usage-line {
        width: 100%;
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }

    WelcomeInformation .welcome-tip-key {
        width: 8;
        height: 1;
        color: $ouro-text-primary;
    }

    WelcomeInformation .welcome-tip-description {
        width: auto;
        height: 1;
        color: $ouro-text-lowlight;
    }

    """


_INPUT_TIPS = (
    ("/", "Commands"),
    ("Ctrl+P", "Actions"),
    ("F1", "Help"),
)
_USAGE_DAY_META = "ouro.welcome_usage.date"


def _blend_hex(background: str, foreground: str, ratio: float) -> str:
    """Blend two Rich-compatible hex colors without depending on terminal palettes."""

    amount = min(1.0, max(0.0, ratio))
    background_rgb = tuple(int(background[index : index + 2], 16) for index in (1, 3, 5))
    foreground_rgb = tuple(int(foreground[index : index + 2], 16) for index in (1, 3, 5))
    blended = tuple(
        round(start + (end - start) * amount)
        for start, end in zip(background_rgb, foreground_rgb, strict=True)
    )
    return "#" + "".join(f"{channel:02x}" for channel in blended)


def _usage_block_colors(values: tuple[int, ...], theme: TuiTheme) -> tuple[str, ...]:
    """Map daily token totals onto a theme-derived dark-to-accent scale."""

    darkest_ratio = 0.28
    if not values:
        return (_blend_hex(theme.background, theme.accent, darkest_ratio),)
    lowest = min(values)
    highest = max(values)
    if lowest == highest:
        return tuple(theme.accent for _ in values)
    return tuple(
        _blend_hex(
            theme.background,
            theme.accent,
            darkest_ratio + (1.0 - darkest_ratio) * ((value - lowest) / (highest - lowest)),
        )
        for value in values
    )


class WelcomeSystem(WelcomeInformation):
    DEFAULT_CSS = """
    WelcomeSystem { width: 100%; overflow: hidden; }
    """

    def __init__(self, status: TuiSystemStatus | None = None) -> None:
        super().__init__(classes="welcome-section welcome-system")
        self._status = status or TuiSystemStatus()
        self._spinner_frame = 0
        self._spinner_timer: Timer | None = None
        self._workspace = Static(classes="welcome-value")
        self._providers = Static(classes="welcome-value")
        self._skills = Static(classes="welcome-value")
        self._plugins = Static(classes="welcome-value")
        self._tasks = Static(id="welcome-task-counts", classes="welcome-value")
        self._mcp_label = Static("MCP", id="welcome-mcp-label", classes="welcome-label")
        self._mcp = Static(classes="welcome-value")

    def compose(self) -> ComposeResult:
        yield Static("SYSTEM", classes="welcome-title")
        for label, value in (
            ("Workspace", self._workspace),
            ("Providers", self._providers),
            ("Skills", self._skills),
            ("Plugins", self._plugins),
            ("Task", self._tasks),
            ("MCP", self._mcp),
        ):
            yield Horizontal(
                self._mcp_label if label == "MCP" else Static(label, classes="welcome-label"),
                value,
                classes="welcome-row",
            )

    def on_mount(self) -> None:
        self._sync_mcp_spinner()
        self._render_values()

    def on_unmount(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None

    def set_system_status(self, status: TuiSystemStatus) -> None:
        self._status = status
        self._sync_mcp_spinner()
        self._render_values()

    def _sync_mcp_spinner(self, *, force: bool = False) -> None:
        if force and self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        theme = theme_for(self)
        if self._status.mcp_connecting_count and len(theme.spinner_frames) > 1:
            if self._spinner_timer is None:
                self._spinner_timer = self.set_interval(
                    theme.spinner_interval_seconds,
                    self._advance_mcp_spinner,
                )
        else:
            if self._spinner_timer is not None:
                self._spinner_timer.stop()
                self._spinner_timer = None
            self._spinner_frame = 0

    def _advance_mcp_spinner(self) -> None:
        self._spinner_frame = (self._spinner_frame + 1) % len(theme_for(self).spinner_frames)
        self._render_mcp_label()

    def _render_mcp_label(self) -> None:
        theme = theme_for(self)
        label = Text()
        if self._status.mcp_connecting_count:
            label.append(
                f"{theme.spinner_frames[self._spinner_frame]} ",
                style=f"bold {theme.accent}",
            )
        label.append("MCP")
        self._mcp_label.update(label)

    def _render_values(self) -> None:
        status = self._status
        theme = theme_for(self)
        counts = status.task_counts
        tasks = Text(
            f"{counts.completed_executions}/"
            f"{counts.completed_executions + counts.failed_executions} completed",
            style=theme.text_primary,
        )
        for label, count, color in (
            ("waiting", counts.waiting, theme.text_lowlight),
            ("running", counts.running, theme.accent),
            ("approval", counts.awaiting_approval, theme.warning),
        ):
            if not count:
                continue
            tasks.append(theme.inline_separator, style=theme.text_lowlight)
            tasks.append(f"{count} {label}", style=color)
        self._tasks.update(tasks)
        self._render_mcp_label()
        if status.task_error:
            self._tasks.update(
                label_text(
                    theme.inline_separator.join(("Unavailable", status.task_error)),
                    theme=theme,
                    style=theme.text_primary,
                )
            )
        self._workspace.update(Text(status.workspace or "not set", style=theme.text_primary))

        providers = Text()
        providers.append(f"{status.model_count} model(s)", style=theme.text_primary)
        if status.model_names:
            providers.append(
                f"{theme.section_separator}{_compact_names(status.model_names)}",
                style=theme.text_lowlight,
            )
        self._providers.update(providers)

        skills = Text(f"{status.skill_count} available", style=theme.text_primary)
        if status.skill_names:
            skills.append(
                f"{theme.section_separator}{_compact_names(status.skill_names)}",
                style=theme.text_lowlight,
            )
        self._skills.update(skills)

        plugins = Text(
            f"{status.plugin_active_count}/{status.plugin_total_count} active",
            style=theme.text_primary,
        )
        if status.plugin_active_names:
            plugins.append(
                f"{theme.section_separator}{_compact_names(status.plugin_active_names)}",
                style=theme.text_lowlight,
            )
        if status.plugin_issue_count:
            plugins.append(theme.section_separator, style=theme.text_lowlight)
            plugins.append(f"{status.plugin_issue_count} need attention", style=theme.warning)
        self._plugins.update(plugins)

        mcp = Text()
        if status.mcp_total_count:
            mcp.append(
                f"{status.mcp_connected_count}/{status.mcp_total_count} connected",
                style=theme.accent,
            )
            mcp.append(theme.section_separator, style=theme.text_lowlight)
            mcp.append(f"{status.mcp_tool_count} tool(s)", style=theme.text_primary)
            if status.mcp_connecting_count:
                mcp.append(theme.section_separator, style=theme.text_lowlight)
                mcp.append(f"{status.mcp_connecting_count} connecting", style=theme.accent)
            if status.mcp_failed_count:
                mcp.append(theme.section_separator, style=theme.text_lowlight)
                mcp.append(f"{status.mcp_failed_count} failed", style=theme.failure)
        else:
            mcp.append("not configured", style=theme.text_lowlight)
        self._mcp.update(mcp)

    def refresh_theme(self) -> None:
        self._spinner_frame = 0
        self._sync_mcp_spinner(force=True)
        self._render_values()


class WelcomeUsage(WelcomeInformation):
    DEFAULT_CSS = """
    WelcomeUsage { width: 1fr; margin-right: 1; }
    """

    def __init__(self, status: TuiSystemStatus | None = None) -> None:
        super().__init__(classes="welcome-section welcome-usage")
        self._status = status or TuiSystemStatus()
        self._chart = Static(classes="welcome-usage-line")
        self._requests = Static(classes="welcome-usage-line")
        self._tokens = Static(classes="welcome-usage-line")
        self._hovered_date: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("USAGE", classes="welcome-title")
        yield self._chart
        yield self._requests
        yield self._tokens

    def on_mount(self) -> None:
        self._render_values()

    def set_system_status(self, status: TuiSystemStatus) -> None:
        self._status = status
        if self._hovered_date not in {day.date for day in status.usage_daily}:
            self._hovered_date = None
        self._render_values()

    def refresh_theme(self) -> None:
        self._render_values()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        date = None
        if event.style is not None:
            candidate = event.style.meta.get(_USAGE_DAY_META)
            if isinstance(candidate, str):
                date = candidate
        self._set_hovered_date(date)

    def on_leave(self, _event: events.Leave) -> None:
        self._set_hovered_date(None)

    def _set_hovered_date(self, date: str | None) -> None:
        if date == self._hovered_date:
            return
        self._hovered_date = date
        self._render_values()

    def _render_values(self) -> None:
        status = self._status
        theme = theme_for(self)
        daily = status.usage_daily
        daily_tokens = tuple(day.total_tokens for day in daily)
        selected = next((day for day in daily if day.date == self._hovered_date), None)
        chart = Text()
        colors = _usage_block_colors(daily_tokens, theme) if daily else ()
        for day, color in zip(daily, colors, strict=True):
            mark = (
                theme.selected_session_node_mark
                if day.date == self._hovered_date
                else theme.session_node_mark
            )
            chart.append(mark, style=RichStyle(color=color, meta={_USAGE_DAY_META: day.date}))
        if daily:
            chart.append(theme.section_separator, style=theme.text_lowlight)
            if selected is not None:
                chart.append(selected.date, style=theme.text_lowlight)
            else:
                days = len(daily)
                chart.append(
                    f"last {days} day{'s' if days != 1 else ''}",
                    style=theme.text_lowlight,
                )
        else:
            chart.append(
                theme.session_node_mark,
                style=_usage_block_colors((), theme)[0],
            )
            chart.append(theme.section_separator, style=theme.text_lowlight)
            chart.append(
                "no records",
                style=theme.text_lowlight,
            )
        self._chart.update(chart)

        request_count = (
            selected.request_count if selected is not None else status.usage_request_count
        )
        requests = Text(
            f"{request_count} request(s)",
            style=theme.text_primary,
        )
        self._requests.update(requests)
        input_tokens = selected.input_tokens if selected is not None else status.usage_input_tokens
        output_tokens = (
            selected.output_tokens if selected is not None else status.usage_output_tokens
        )
        cache_read_tokens = (
            selected.cache_read_tokens if selected is not None else status.usage_cache_read_tokens
        )
        tokens = Text(f"{format_compact_count(input_tokens)} in", style=theme.text_lowlight)
        for value in (
            f"{format_compact_count(output_tokens)} out",
            f"{_cache_hit_rate(cache_read_tokens, input_tokens):.2f}% cache",
        ):
            tokens.append(theme.inline_separator, style=theme.text_lowlight)
            tokens.append(value, style=theme.text_lowlight)
        self._tokens.update(tokens)


def _cache_hit_rate(cache_read_tokens: int, input_tokens: int) -> float:
    return cache_read_tokens / input_tokens * 100 if input_tokens else 0.0


class WelcomeTips(WelcomeInformation):
    DEFAULT_CSS = """
    WelcomeTips { width: auto; min-width: 20; }
    """

    def __init__(self) -> None:
        super().__init__(classes="welcome-section welcome-tips")
        self._title = Static(classes="welcome-title")

    def compose(self) -> ComposeResult:
        yield self._title
        for key, description in _INPUT_TIPS:
            yield Horizontal(
                Static(key, classes="welcome-tip-key"),
                Static(description, classes="welcome-tip-description"),
                classes="welcome-row",
            )

    def on_mount(self) -> None:
        self.refresh_theme()

    def refresh_theme(self) -> None:
        theme = theme_for(self)
        self._title.update(
            Text(f"{theme.selected_choice_marker} TIPS", style=f"bold {theme.accent}")
        )


class WelcomeVersion(Static):
    """Optional package-owned version label."""

    DEFAULT_CSS = """
    WelcomeVersion {
        width: 100%;
        height: 1;
        padding: 0 2;
        color: $ouro-text-lowlight;
        text-style: none;
        text-align: right;
    }

    """

    def __init__(self) -> None:
        super().__init__(f"v{__version__}", id="welcome-version")
