from __future__ import annotations

from rich.cells import get_character_cell_size
from rich.text import Text
from textual.binding import Binding
from textual.widgets import OptionList

from ouro.tui.screens import Choice, ChoiceEditHandler, EditableChoiceScreen
from ouro.tui.theme import DEFAULT_TUI_THEME, theme_for


class MultilineEditableChoiceScreen(EditableChoiceScreen):
    """Reusable inline form with multiline fields declared by ``Choice.edit_lines``.

    Fields with one line retain the ordinary inline editor. Multiline fields
    own their newline, line navigation, paste and fixed-height viewport behavior;
    the shared menu continues to own selection, save and cancellation.
    """

    MULTILINE_EDIT_HINT = DEFAULT_TUI_THEME.inline_separator.join(
        ("Type edit", "↑/↓/←/→ cursor", "Enter save", "Shift+Enter new line", "Esc cancel")
    )
    BINDINGS = [Binding("shift+enter", "edit_newline", "New line", show=False, priority=True)]

    def __init__(
        self,
        *,
        title: str,
        placeholder: str,
        choices: tuple[Choice, ...],
        on_save: ChoiceEditHandler,
    ) -> None:
        self._edit_lines = 1
        self._edit_view_row = 0
        super().__init__(title=title, placeholder=placeholder, choices=choices, on_save=on_save)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "edit_newline":
            return self._editing_value is not None and self._edit_lines > 1
        if self._editing_value is not None and action in {"previous_group", "next_group"}:
            return True
        return super().check_action(action, parameters)

    def _begin_edit(self, selected: str) -> None:
        choice = next((choice for choice in self._choices if choice.value == selected), None)
        if choice is None or choice.disabled:
            return
        self._edit_lines = choice.edit_lines
        self._edit_view_row = 0
        super()._begin_edit(selected)
        if self._edit_lines > 1:
            self._show_hint(self.MULTILINE_EDIT_HINT)

    def _insert_paste(self, value: str) -> None:
        if self._edit_lines == 1:
            super()._insert_paste(value)
        else:
            self._insert_text(value.replace("\r\n", "\n").replace("\r", "\n"))

    def action_edit_newline(self) -> None:
        if self._editing_value is not None and self._edit_lines > 1:
            self._insert_text("\n")

    def action_previous_choice(self) -> None:
        if self._editing_value is not None and self._edit_lines > 1:
            self._move_edit_line(-1)
        else:
            super().action_previous_choice()

    def action_next_choice(self) -> None:
        if self._editing_value is not None and self._edit_lines > 1:
            self._move_edit_line(1)
        else:
            super().action_next_choice()

    def _move_edit_line(self, direction: int) -> None:
        rows = edit_row_ranges(self._edit_text, self._editor_width())
        current = next(
            index for index, (start, end) in enumerate(rows) if start <= self._edit_cursor < end
        )
        target = max(0, min(current + direction, len(rows) - 1))
        start, end = rows[target]
        self._edit_cursor = min(start + self._edit_cursor - rows[current][0], end - 1)
        self._reset_edit_cursor()

    def action_edit_home(self) -> None:
        if self._editing_value is not None and self._edit_lines > 1:
            self._edit_cursor = self._edit_text.rfind("\n", 0, self._edit_cursor) + 1
            self._reset_edit_cursor()
        else:
            super().action_edit_home()

    def action_edit_end(self) -> None:
        if self._editing_value is not None and self._edit_lines > 1:
            end = self._edit_text.find("\n", self._edit_cursor)
            self._edit_cursor = end if end >= 0 else len(self._edit_text)
            self._reset_edit_cursor()
        else:
            super().action_edit_end()

    def _render_editor(self) -> None:
        if self._edit_lines == 1:
            super()._render_editor()
            return
        if self._editing_value is None:
            return
        options = self.query_one("#choice-list", OptionList)
        theme = theme_for(self)
        prompt, self._edit_view_row = multiline_edit_prompt(
            self._editor_display_text(self._edit_text),
            cursor=self._edit_cursor,
            width=self._editor_width(),
            height=self._edit_lines,
            first_row=self._edit_view_row,
            cursor_style=f"not bold reverse {theme.accent}"
            if self._edit_cursor_visible
            else "not bold",
        )
        options.replace_option_prompt(self._editing_value, prompt)


def edit_row_ranges(value: str, width: int) -> tuple[tuple[int, int], ...]:
    """Wrap editor rows by terminal cells while retaining source cursor offsets.

    A newline and the end of the document each reserve a cursor cell. Their
    offsets remain addressable even when the preceding row fills the viewport.
    """

    rows = []
    start = cells = 0
    display = value + " "
    for index, character in enumerate(display):
        size = get_character_cell_size(" " if character in "\n\t" else character)
        if cells + size > width and index > start:
            rows.append((start, index))
            start, cells = index, 0
        cells += size
        if character == "\n":
            rows.append((start, index + 1))
            start, cells = index + 1, 0
    if start < len(display):
        rows.append((start, len(display)))
    return tuple(rows)


def multiline_edit_prompt(
    value: str,
    *,
    cursor: int,
    width: int,
    height: int,
    first_row: int,
    cursor_style: str,
) -> tuple[Text, int]:
    """Render a fixed-height window that follows the inline edit cursor."""

    rows = edit_row_ranges(value, width)
    cursor_row = next(index for index, (start, end) in enumerate(rows) if start <= cursor < end)
    first_row = max(0, min(first_row, cursor_row, len(rows) - height))
    if cursor_row >= first_row + height:
        first_row = cursor_row - height + 1
    display = (value + " ").replace("\n", " ").replace("\t", " ")
    lines = []
    for start, end in rows[first_row : first_row + height]:
        line = Text(display[start:end], style="not bold", no_wrap=True)
        if start <= cursor < end:
            line.stylize(cursor_style, cursor - start, cursor - start + 1)
        lines.append(line)
    lines.extend(Text(" ") for _ in range(height - len(lines)))
    return Text("\n", no_wrap=True).join(lines), first_row
