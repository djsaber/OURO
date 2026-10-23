"""Editable menu values, validation and clipboard actions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from rich.cells import get_character_cell_size
from rich.text import Text
from textual import events, work
from textual.binding import Binding
from textual.timer import Timer
from textual.widgets import Input, OptionList

from ouro.tui.clipboard import ClipboardReadError, read_system_clipboard
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.models import Choice, ChoiceEditHandler, ChoiceTabHandler
from ouro.tui.menus.rows import _ChoiceOptionList
from ouro.tui.theme import DEFAULT_TUI_THEME, theme_for


class _EditableChoiceList(_ChoiceOptionList):
    """Reserve right-click for paste without duplicating OptionList activation."""

    async def _on_click(self, event: events.Click) -> None:
        if event.button == 3:
            event.prevent_default()
            event.stop()

        # Textual also dispatches OptionList._on_click through the MRO.
        # Calling super here would activate one left-click twice.


class EditableChoiceScreen(ChoiceScreen):
    """Edit the selected choice label in place without opening another screen."""

    CURSOR_BLINK_INTERVAL = 0.5
    DEFAULT_HINT = DEFAULT_TUI_THEME.inline_separator.join(
        ("↑/↓ move", "←/→ group", "Enter edit", "Esc close")
    )
    BACK_HINT = DEFAULT_TUI_THEME.inline_separator.join(
        ("↑/↓ move", "←/→ group", "Enter edit", "Esc back")
    )
    EDIT_HINT = DEFAULT_TUI_THEME.inline_separator.join(
        ("Type edit", "←/→ cursor", "Enter save", "Esc cancel")
    )

    BINDINGS = [
        Binding("up", "previous_choice", "Previous", show=False, priority=True),
        Binding("down", "next_choice", "Next", show=False, priority=True),
        Binding("left", "previous_group", "Cursor left", show=False, priority=True),
        Binding("right", "next_group", "Cursor right", show=False, priority=True),
        Binding("home", "edit_home", "Start", show=False, priority=True),
        Binding("end", "edit_end", "End", show=False, priority=True),
        Binding("ctrl+u", "edit_clear", "Clear", show=False, priority=True),
        Binding("backspace", "edit_backspace", "Erase", show=False, priority=True),
        Binding("delete", "edit_delete", "Delete", show=False, priority=True),
        Binding("enter", "accept_choice", "Edit / save", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(
        self,
        *,
        title: str,
        placeholder: str,
        choices: tuple[Choice, ...],
        on_save: ChoiceEditHandler,
        on_tab: ChoiceTabHandler | None = None,
        interaction_hint: str | None = None,
    ) -> None:
        super().__init__(
            title=title,
            placeholder=placeholder,
            choices=choices,
            on_tab=on_tab,
            interaction_hint=interaction_hint,
        )
        self._on_save = on_save
        self._editing_value: str | None = None
        self._edit_text = ""
        self._edit_original_text = ""
        self._edit_cursor = 0
        self._edit_view_start = 0
        self._edit_cursor_visible = True
        self._edit_cursor_timer: Timer | None = None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "switch_domain" and self._editing_value is not None:
            return False
        if action in {"previous_group", "next_group"} and self._editing_value is not None:
            return True
        if action.startswith("edit_"):
            return self._editing_value is not None
        return super().check_action(action, parameters)

    def on_unmount(self) -> None:
        self._stop_edit_cursor()
        super().on_unmount()

    def _choice_is_editing(self, value: str) -> bool:
        return self._editing_value == value

    def on_resize(self, event: events.Resize) -> None:
        super().on_resize(event)
        if self._editing_value is not None:
            self.call_after_refresh(self._render_editor)

    def _choice_list(self) -> OptionList:
        return _EditableChoiceList(
            *self._options(
                self._choices,
                group_accent=theme_for(self).accent,
                theme=theme_for(self),
            ),
            id="choice-list",
            classes="grouped-menu-list",
        )

    async def _select_choice(self, selected: str) -> None:
        if self._editing_value is None:
            self._begin_edit(selected)
            return
        if selected != self._editing_value:
            return
        if self._edit_text == self._edit_original_text:
            self._finish_edit(self._choices, selected=selected)
            return
        await self._save_edit(self._on_save(selected, self._edit_text), selected=selected)

    def _edit_snapshot(self) -> tuple[str | None, ...]:
        return (self._editing_value, self._edit_text)

    async def _save_edit(
        self, save: Awaitable[tuple[Choice, ...] | None], *, selected: str
    ) -> None:
        revision, edit = self._page_revision, self._edit_snapshot()
        choices = await save
        if choices is None or not self._accepts_choice_updates() or revision != self._page_revision:
            return
        if self._edit_snapshot() == edit:
            self._finish_edit(choices, selected=selected)
            return
        self.update_choices(choices, preferred_value=self._selected_value())
        if self._editing_value is not None:
            saved = next((choice for choice in choices if choice.value == selected), None)
            if saved is not None and self._editing_value == selected:
                self._edit_original_text = (
                    saved.edit_value if saved.edit_value is not None else saved.label
                )
            self._render_editor()

    def on_key(self, event: events.Key) -> None:
        if self._editing_value is None or not event.is_printable or event.character is None:
            return
        event.stop()
        event.prevent_default()
        self._insert_text(event.character)

    def on_paste(self, event: events.Paste) -> None:
        if self._editing_value is None:
            return
        event.stop()
        event.prevent_default()
        self._insert_paste(event.text)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 3 or self._editing_value is None:
            return
        event.prevent_default()
        event.stop()
        self._paste_system_clipboard()

    @work(group="editable-choice-clipboard")
    async def _paste_system_clipboard(self) -> None:
        try:
            value = await asyncio.to_thread(read_system_clipboard)
        except ClipboardReadError as exc:
            self.notify(str(exc), severity="warning", markup=False)
            return
        if self._editing_value is not None:
            self._insert_paste(value)

    def _insert_paste(self, value: str) -> None:
        self._insert_text(" ".join(value.splitlines()))

    def action_previous_choice(self) -> None:
        if self._editing_value is None:
            super().action_previous_choice()

    def action_next_choice(self) -> None:
        if self._editing_value is None:
            super().action_next_choice()

    def action_previous_group(self) -> None:
        if self._editing_value is None:
            super().action_previous_group()
            return
        self._edit_cursor = max(0, self._edit_cursor - 1)
        self._reset_edit_cursor()

    def action_next_group(self) -> None:
        if self._editing_value is None:
            super().action_next_group()
            return
        self._edit_cursor = min(len(self._edit_text), self._edit_cursor + 1)
        self._reset_edit_cursor()

    def action_edit_home(self) -> None:
        if self._editing_value is not None:
            self._edit_cursor = 0
            self._reset_edit_cursor()

    def action_edit_end(self) -> None:
        if self._editing_value is not None:
            self._edit_cursor = len(self._edit_text)
            self._reset_edit_cursor()

    def action_edit_clear(self) -> None:
        if self._editing_value is not None:
            self._edit_text = ""
            self._edit_cursor = 0
            self._reset_edit_cursor()

    def action_edit_backspace(self) -> None:
        if self._editing_value is None or self._edit_cursor == 0:
            return
        self._edit_text = (
            self._edit_text[: self._edit_cursor - 1] + self._edit_text[self._edit_cursor :]
        )
        self._edit_cursor -= 1
        self._reset_edit_cursor()

    def action_edit_delete(self) -> None:
        if self._editing_value is None or self._edit_cursor >= len(self._edit_text):
            return
        self._edit_text = (
            self._edit_text[: self._edit_cursor] + self._edit_text[self._edit_cursor + 1 :]
        )
        self._reset_edit_cursor()

    async def action_cancel(self) -> None:
        if self._editing_value is None:
            await super().action_cancel()
            return
        selected = self._editing_value
        self._finish_edit(self._choices, selected=selected)

    def _begin_edit(self, selected: str) -> None:
        choice = next((choice for choice in self._choices if choice.value == selected), None)
        if choice is None or choice.disabled:
            return
        self._editing_value = selected
        self._edit_text = choice.edit_value if choice.edit_value is not None else choice.label
        self._edit_original_text = self._edit_text
        self._edit_cursor = len(self._edit_text)
        self._edit_view_start = 0
        self.query_one("#choice-filter", Input).disabled = True
        self._show_hint(self.EDIT_HINT)
        self._start_edit_cursor()
        self._render_editor()

    def _finish_edit(self, choices: tuple[Choice, ...], *, selected: str) -> None:
        self._stop_edit_cursor()
        self._editing_value = None
        self._edit_text = ""
        self._edit_original_text = ""
        self._edit_cursor = 0
        self._edit_view_start = 0
        self._choices = choices
        filter_input = self.query_one("#choice-filter", Input)
        filter_input.disabled = False
        needle = filter_input.value
        selected_choice = next(
            (choice for choice in choices if choice.value == selected),
            None,
        )
        if selected_choice is not None and not self._matches_filter(
            selected_choice,
            needle.casefold().strip(),
        ):
            filter_input.value = ""
            needle = ""
        self._show_hint()
        self._refresh_options(needle=needle, preferred_value=selected)
        filter_input.focus()

    def _replace_choices(self, choices: tuple[Choice, ...], *, selected: str) -> None:
        """Replace the live menu projection while preserving filter and focus."""

        if not self._accepts_choice_updates():
            return
        self._choices = choices
        self._refresh_options(
            needle=self.query_one("#choice-filter", Input).value,
            preferred_value=selected,
        )
        self.query_one("#choice-list", OptionList).focus()

    def _insert_text(self, value: str) -> None:
        if not value:
            return
        self._edit_text = (
            self._edit_text[: self._edit_cursor] + value + self._edit_text[self._edit_cursor :]
        )
        self._edit_cursor += len(value)
        self._reset_edit_cursor()

    def _start_edit_cursor(self) -> None:
        self._stop_edit_cursor()
        self._edit_cursor_visible = True
        self._edit_cursor_timer = self.set_interval(
            self.CURSOR_BLINK_INTERVAL,
            self._toggle_edit_cursor,
        )

    def _stop_edit_cursor(self) -> None:
        if self._edit_cursor_timer is not None:
            self._edit_cursor_timer.stop()
            self._edit_cursor_timer = None
        self._edit_cursor_visible = True

    def _reset_edit_cursor(self) -> None:
        self._edit_cursor_visible = True
        if self._edit_cursor_timer is not None:
            self._edit_cursor_timer.reset()
        self._render_editor()

    def _toggle_edit_cursor(self) -> None:
        if self._editing_value is None:
            self._stop_edit_cursor()
            return
        self._edit_cursor_visible = not self._edit_cursor_visible
        self._render_editor()

    def _render_editor(self) -> None:
        editing_value = self._editing_value
        if editing_value is None:
            return
        options = self.query_one("#choice-list", OptionList)
        option_index = next(
            (
                index
                for index in range(options.option_count)
                if options.get_option_at_index(index).id == editing_value
            ),
            None,
        )
        if option_index is None:
            return
        theme = theme_for(self)
        accent = theme.accent
        display_text = self._editor_display_text(self._edit_text)
        prompt = Text(no_wrap=True)
        cursor_style = f"not bold reverse {accent}" if self._edit_cursor_visible else "not bold"
        if not display_text:
            prompt.append(" ", style=cursor_style)
            placeholder = self._editor_placeholder()
            if placeholder:
                prompt.append(
                    " ".join(placeholder.splitlines()),
                    style=f"not bold {theme.text_lowlight} italic",
                )
            prompt.truncate(self._editor_width(), overflow="ellipsis")
            options.replace_option_prompt_at_index(option_index, self._editor_prompt(prompt))
            return
        editor_width = self._editor_width()
        view_start, view_end = self._editor_view_bounds(display_text, editor_width)
        if view_start:
            prompt.append("…", style=f"not bold {theme.text_lowlight}")
        prompt.append(display_text[view_start : self._edit_cursor], style="not bold")
        if self._edit_cursor < len(display_text):
            prompt.append(
                display_text[self._edit_cursor],
                style=cursor_style,
            )
            prompt.append(
                display_text[self._edit_cursor + 1 : view_end],
                style="not bold",
            )
        else:
            prompt.append(" ", style=cursor_style)
        if view_end < len(display_text):
            prompt.append("…", style=f"not bold {theme.text_lowlight}")
        options.replace_option_prompt_at_index(option_index, self._editor_prompt(prompt))

    def _editor_placeholder(self) -> str:
        choice = next((c for c in self._choices if c.value == self._editing_value), None)
        return choice.edit_placeholder if choice is not None else ""

    def _editor_trailing(self) -> str:
        return ""

    def _editor_prompt(self, prompt: Text) -> Text:
        trailing = self._editor_trailing()
        if trailing:
            prompt.pad_right(max(0, self._editor_width() - prompt.cell_len) + 2)
            prompt.append(trailing, style=f"not bold {theme_for(self).text_lowlight}")
        return prompt

    def _editor_width(self) -> int:
        options = self.query_one("#choice-list", OptionList)
        trailing = self._editor_trailing()
        return max(
            1,
            options.scrollable_content_region.width
            - options.get_component_styles("option-list--option").padding.width
            - (Text(trailing).cell_len + 2 if trailing else 0),
        )

    def _editor_view_bounds(self, display_text: str, width: int) -> tuple[int, int]:
        """Return a stable single-line window that always contains the edit cursor."""

        cursor = min(self._edit_cursor, len(display_text))
        start = min(self._edit_view_start, cursor)
        end = cursor + 1 if cursor < len(display_text) else len(display_text)
        cell_offsets = [0]
        for character in display_text:
            cell_offsets.append(cell_offsets[-1] + get_character_cell_size(character))

        def view_width(candidate_start: int, candidate_end: int) -> int:
            return (
                (1 if candidate_start else 0)
                + cell_offsets[candidate_end]
                - cell_offsets[candidate_start]
                + (1 if candidate_end < len(display_text) else 0)
                + (1 if cursor == len(display_text) else 0)
            )

        while start < cursor and view_width(start, end) > width:
            start += 1
        while end < len(display_text) and view_width(start, end + 1) <= width:
            end += 1
        while start > 0 and view_width(start - 1, end) <= width:
            start -= 1

        self._edit_view_start = start
        return start, end

    def _editor_display_text(self, value: str) -> str:
        """Return the visible editor text; subclasses may mask sensitive values."""

        return value
