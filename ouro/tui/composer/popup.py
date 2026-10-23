"""Command and skill completion popup presentation."""

from __future__ import annotations

from collections.abc import Callable

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.geometry import Region, Size
from textual.strip import Strip
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from ouro.tui.commands import TuiCommand, TuiCommandArgument, argument_suggestion
from ouro.tui.text_wrapping import _SINGLE_LINE_TRANSLATION
from ouro.tui.theme import theme_for

COMMAND_POPUP_MIN_HEIGHT = 3


COMMAND_POPUP_MAX_HEIGHT = 14


class _CommandPopupRows(Widget):
    """One logical item per line; Textual requests only the visible strips."""

    def __init__(self, render_row: Callable[[int, int], Text]) -> None:
        super().__init__(id="command-popup-content")
        self.row_count = 0
        self._render_row = render_row

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return self.row_count

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        style = self.visual_style.rich_style
        if not 0 <= y < self.row_count:
            return Strip.blank(width, style)
        row = self._render_row(y, width)
        row.plain = row.plain.translate(_SINGLE_LINE_TRANSLATION)
        row.truncate(width, overflow="ellipsis", pad=True)
        return Strip(row.render(self.app.console, end=""), width).apply_style(style)


class CommandPopup(VerticalScroll):
    """Composer-controlled command panel with keyboard-owned hidden scrolling."""

    def __init__(self) -> None:
        super().__init__(id="command-popup")
        self.display = False
        self._commands: tuple[TuiCommand, ...] = ()
        self._argument_command: TuiCommand | None = None
        self._arguments: tuple[TuiCommandArgument, ...] = ()
        self._argument_query = ""
        self._argument_input_hint = ""
        self._selected: int | None = 0
        self._content = _CommandPopupRows(self._render_row)
        self._label_width = 0
        self._has_selection_marks = False
        self._heading_widget = Static(id="command-popup-heading")
        self._heading_widget.display = False
        self._heading = ""
        self._selection_count: int | None = None
        self._hint = ""
        self._loading = False
        self._spinner_frame = 0
        self._spinner_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield self._heading_widget
        yield self._content

    @property
    def selected_command(self) -> TuiCommand | None:
        if self._argument_command is not None or not self._commands or self._selected is None:
            return None
        return self._commands[self._selected]

    @property
    def argument_command(self) -> TuiCommand | None:
        return self._argument_command

    @property
    def selected_argument(self) -> TuiCommandArgument | None:
        if self._argument_command is None or not self._arguments or self._selected is None:
            return None
        return self._arguments[self._selected]

    def suggestion_for(self, command_text: str) -> str | None:
        """Reuse the displayed candidates only while the input still matches them."""
        command = self._argument_command
        name, separator, query = command_text.partition(" ")
        if (
            not self.display
            or command is None
            or not separator
            or name.casefold()
            not in (value.casefold() for value in (command.name, *command.aliases))
            or query != self._argument_query
        ):
            return None
        if not query.strip():
            return self._argument_input_hint
        selected = self.selected_argument or (self._arguments[0] if self._arguments else None)
        return argument_suggestion(command, query, selected)

    def show_commands(self, commands: tuple[TuiCommand, ...]) -> None:
        self.set_loading(False)
        if not commands:
            self.hide()
            return
        with self.app.batch_update():
            selected_name = (
                self.selected_command.name if self.selected_command is not None else None
            )
            self._commands = commands
            self._argument_command = None
            self._arguments = ()
            self._heading = self._hint = ""
            self._selection_count = None
            self._selected = next(
                (index for index, item in enumerate(commands) if item.name == selected_name),
                0,
            )
            self.display = True
            self._label_width = max(cell_len(command.name) for command in commands)
            self._refresh_rows()

    def show_arguments(
        self,
        command: TuiCommand,
        arguments: tuple[TuiCommandArgument, ...],
        *,
        query: str = "",
        heading: str = "",
        selection_count: int | None = None,
        hint: str = "",
    ) -> None:
        """Show matches without implicitly accepting suggestions over typed input."""

        self.set_loading(False)
        with self.app.batch_update():
            selected_value = (
                self.selected_argument.value if self.selected_argument is not None else None
            )
            self._commands = ()
            self._argument_command = command
            self._arguments = arguments
            self._argument_query = query
            self._argument_input_hint = command.input_hint or ""
            self._heading, self._hint = heading, hint
            self._selection_count = selection_count
            self._label_width = max((cell_len(item.display_label) for item in arguments), default=0)
            self._has_selection_marks = any(item.checked is not None for item in arguments)
            self._selected = None
            if not query.strip() or not any(item.is_suggestion for item in arguments):
                self._selected = next(
                    (index for index, item in enumerate(arguments) if item.value == selected_value),
                    0,
                )
            self.display = bool(heading) or bool(arguments) or not query.strip()
            if self.display:
                self._refresh_rows()

    def hide(self) -> None:
        self.set_loading(False)
        if (
            not self.display
            and not self._commands
            and self._argument_command is None
            and not self._arguments
            and self._selected == 0
        ):
            return
        with self.app.batch_update():
            self.display = False
            self._commands = ()
            self._argument_command = None
            self._arguments = ()
            self._selected = 0
            self._heading = self._hint = ""
            self._selection_count = None
            self._heading_widget.display = False

    def navigate(self, offset: int) -> None:
        items = self._arguments if self._argument_command is not None else self._commands
        if not items:
            return
        previous = self._selected
        if self._selected is None:
            self._selected = 0 if offset > 0 else len(items) - 1
        else:
            self._selected = (self._selected + offset) % len(items)
        for index in (previous, self._selected):
            if index is not None:
                self._content.refresh(Region(0, index, self._content.size.width, 1))
        self._scroll_selected_into_view()

    def refresh_theme(self) -> None:
        if self._loading:
            self.set_loading(False)
            self.set_loading(True)
        self._refresh_rows()

    def set_loading(self, loading: bool) -> None:
        """Animate the existing row marker while retaining the complete popup."""

        if self._loading == loading:
            return
        self._loading = loading
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self._spinner_frame = 0
        theme = theme_for(self)
        if loading and len(theme.spinner_frames) > 1:
            self._spinner_timer = self.set_interval(
                theme.spinner_interval_seconds, self._advance_loading_animation
            )
        self._refresh_loading_row()

    def _refresh_loading_row(self) -> None:
        self._content.refresh(Region(0, self._selected or 0, self._content.size.width, 1))

    def _advance_loading_animation(self) -> None:
        self._spinner_frame = (self._spinner_frame + 1) % len(theme_for(self).spinner_frames)
        self._refresh_loading_row()

    def on_unmount(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None

    def on_resize(self, event: events.Resize) -> None:
        self._refresh_rows()

    def _refresh_rows(self) -> None:
        count = len(self._arguments) if self._argument_command is not None else len(self._commands)
        row_count = max(1, count) + bool(self._hint)
        resized = row_count != self._content.row_count
        self._content.row_count = row_count
        self._heading_widget.display = bool(self._heading)
        self._render_heading()
        self._content.refresh(layout=resized)
        # Data changes and terminal resizing need the new layout before scrolling.
        self.call_after_refresh(self._scroll_selected_into_view)

    def _render_heading(self) -> None:
        if not self._heading:
            return
        width = self.scrollable_content_region.width
        theme = theme_for(self)
        heading = Text(self._heading, style=f"bold {theme.text_emphasis}")
        if self._selection_count is not None:
            heading = _trailing_badge(
                heading, str(self._selection_count), width, theme.text_lowlight
            )
        if width > 0:
            heading.truncate(width, overflow="ellipsis", pad=True)
        self._heading_widget.update(heading)

    def _render_row(self, index: int, width: int) -> Text:
        theme = theme_for(self)
        marker = f"{theme.spinner_frames[self._spinner_frame]} " if self._loading else "› "
        if self._hint and index == self._content.row_count - 1:
            return Text(self._hint, style=theme.text_lowlight)
        if self._argument_command is not None and not self._arguments:
            return Text.assemble(
                (marker, theme.accent), (self._argument_command.title, theme.text_primary)
            )
        selected = index == self._selected
        row = Text(style=f"on {theme.highlight}" if selected else "")
        row.append(marker if selected else "  ", style=theme.accent if selected else "")
        if self._argument_command is None:
            if not self._commands:
                return Text()
            command = self._commands[index]
            row.append(
                command.name + " " * (self._label_width - cell_len(command.name)),
                style=theme.accent_bright if selected else theme.text_primary,
            )
            row.append(
                f"  {command.title}",
                style=theme.text_primary if selected else theme.text_lowlight,
            )
            return row
        argument = self._arguments[index]
        if argument.checked is not None:
            marker = (
                theme.selected_choice_marker if argument.checked else theme.unselected_choice_marker
            )
            row.append(
                f"{marker} ",
                style=theme.accent if argument.checked else theme.text_lowlight,
            )
        elif self._has_selection_marks:
            row.append("  ")
        label = argument.display_label.translate(_SINGLE_LINE_TRANSLATION)
        row.append(
            label,
            style=argument.accent or (theme.accent_bright if selected else theme.text_primary),
        )
        if argument.description:
            row.append(" " * max(0, self._label_width - cell_len(label)))
            row.append(
                "  " + argument.description,
                style=theme.text_primary if selected else theme.text_lowlight,
            )
        if argument.trailing_badge:
            row = _trailing_badge(row, argument.trailing_badge, width, theme.accent)
        return row

    def _scroll_selected_into_view(self) -> None:
        count = len(self._arguments) if self._argument_command is not None else len(self._commands)
        if not self.display or self._selected is None or self._selected >= count:
            return
        # Every item is truncated to one terminal row. Its position is its index,
        # independent of directory size, label length, and display-cell width.
        spacing = self._content.dock_gutter
        self.scroll_to_region(
            Region(0, spacing.top + self._selected, self.scrollable_content_region.width, 1),
            animate=False,
            force=True,
            x_axis=False,
            spacing=spacing,
            immediate=True,
        )


def _trailing_badge(row: Text, label: str, width: int, accent: str) -> Text:
    return _trailing_label(row, f"[{label}]", width, accent)


def _trailing_label(row: Text, label: str, width: int, accent: str) -> Text:
    badge = Text(label, style=accent)
    if width > 0:
        row.truncate(max(0, width - badge.cell_len - 1), overflow="ellipsis", pad=True)
    row.append(" ")
    row.append_text(badge)
    return row
