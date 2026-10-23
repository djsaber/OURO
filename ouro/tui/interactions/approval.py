"""Tool approval choices and keyboard interaction."""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Static

from ouro.tui.theme import theme_for

INTERACTION_PANEL_MIN_HEIGHT = 6


INTERACTION_PANEL_MAX_HEIGHT = 10


class ApprovalPanel(Vertical):
    """Shared keyboard and visual skeleton for inline approval workflows."""

    can_focus = True
    can_focus_children = False
    BINDINGS = [
        Binding("up", "previous_option", show=False),
        Binding("down", "next_option", show=False),
        Binding("enter", "accept", "Choose", show=False),
        Binding("tab", "retain_focus", show=False, priority=True),
    ]

    def __init__(self, *, panel_id: str, title_id: str, controls_id: str) -> None:
        super().__init__(id=panel_id, classes="approval-panel")
        self._title = Static(id=title_id, classes="approval-title")
        self._controls = Static(id=controls_id, classes="approval-controls")
        self._cursor = 0

    def action_previous_option(self) -> None:
        self._move_approval_cursor(-1)

    def action_next_option(self) -> None:
        self._move_approval_cursor(1)

    def action_retain_focus(self) -> None:
        self.focus()

    def _move_approval_cursor(self, offset: int) -> None:
        choice_count = self._approval_choice_count()
        if not choice_count or self.disabled:
            return
        self._cursor = (self._cursor + offset) % choice_count
        self._render_approval_controls()
        self.call_after_refresh(self._scroll_approval_selection_into_view)

    def _scroll_approval_selection_into_view(self) -> None:
        if self._cursor == 0:
            self.scroll_home(animate=False)
        else:
            self.scroll_end(animate=False)

    def _render_approval_title(self, title: str) -> None:
        theme = theme_for(self)
        self._title.update(Text(title, style=f"bold {theme.text_emphasis}"))

    def _append_approval_option(
        self,
        content: Text,
        *,
        index: int,
        label: str,
        description: str,
        tone: str,
    ) -> None:
        active = index == self._cursor
        theme = theme_for(self)
        content.append("› " if active else "  ", style=theme.accent if active else "")
        content.append(
            label,
            style=f"bold {tone}" if active else theme.text_primary,
        )
        if description:
            content.append(
                f"  {description}",
                style=theme.text_lowlight,
            )
        content.append("\n")

    def _append_approval_hint(
        self,
        content: Text,
        *,
        leading: tuple[str, ...] = (),
        trailing: tuple[str, ...] = (),
    ) -> None:
        content.append(
            "\n"
            + theme_for(self).inline_separator.join(
                (*leading, "↑/↓ move", "Enter choose", *trailing)
            ),
            style=theme_for(self).text_lowlight,
        )

    def _approval_choice_count(self) -> int:
        raise NotImplementedError

    def _render_approval_controls(self) -> None:
        raise NotImplementedError
