"""Shared terminal brand header."""

from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.widgets import Static

from ouro.core.state import DEFAULT_SESSION_TITLE
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, label_text, theme_for

_HEADER_MIN_GAP = 3


class BrandHeader(Static):
    """Session title and workspace docked at the top of the screen."""

    def __init__(
        self,
        *,
        initial_context: str = DEFAULT_SESSION_TITLE,
        initial_workspace: str | None = None,
    ) -> None:
        super().__init__(id="brand-header")
        self._session_title = initial_context
        self._session_detail: str | None = None
        self._workspace = initial_workspace

    def on_mount(self) -> None:
        self._render_header()

    def on_resize(self, event: events.Resize) -> None:
        self._render_header()

    def set_context(
        self,
        *,
        session_title: str | None = None,
        session_detail: str | None = None,
        workspace: str | None = None,
    ) -> None:
        if session_title is not None:
            self._session_title = session_title
        self._session_detail = session_detail
        if workspace is not None:
            self._workspace = workspace
        self._render_header()

    def refresh_theme(self) -> None:
        self._render_header()

    def _render_header(self) -> None:
        theme = theme_for(self)
        self.update(
            _brand_header_content(
                self._session_title,
                self._session_detail,
                self._workspace,
                width=self.size.width,
                theme=theme,
            )
        )


def _brand_header_content(
    session_title: str,
    session_detail: str | None,
    workspace: str | None,
    *,
    width: int,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> Text:
    """Lay out the title and right-aligned workspace with workspace priority."""

    content = Text()
    content.append(f"{theme.brand_mark} ", style=f"bold {theme.accent}")
    title = label_text(session_title, theme=theme, style=theme.text_emphasis)
    if session_detail:
        title.append(theme.inline_separator, style=theme.text_lowlight)
        title.append(session_detail, style=theme.text_lowlight)
    if width <= 0:
        content.append_text(title)
        if workspace:
            content.append(" " * _HEADER_MIN_GAP)
            content.append(workspace, style=theme.text_lowlight)
        return content

    content.truncate(width, overflow="ellipsis")
    remaining = width - content.cell_len
    if remaining <= 0:
        return content

    if not workspace:
        title.truncate(remaining, overflow="ellipsis")
        content.append_text(title)
        return content

    workspace_width = cell_len(workspace)
    title_width = remaining - workspace_width - _HEADER_MIN_GAP
    if title_width > 0:
        title.truncate(title_width, overflow="ellipsis")
        content.append_text(title)
        content.append(" " * (width - content.cell_len - workspace_width))
        content.append(workspace, style=theme.text_lowlight)
        return content

    # When both fields cannot fit, omit the title before shortening the workspace.
    workspace_gap = min(_HEADER_MIN_GAP, max(1, remaining - workspace_width))
    workspace_text = Text(workspace, style=theme.text_lowlight)
    workspace_text.truncate(max(0, remaining - workspace_gap), overflow="ellipsis")
    content.append(" " * workspace_gap)
    content.append_text(workspace_text)
    return content
