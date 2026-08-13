from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Literal, TypeVar

from rich.table import Table
from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ouro.tui.clipboard import ClipboardReadError, read_system_clipboard
from ouro.tui.commands import slash_command_help
from ouro.tui.text_inputs import TuiInput
from ouro.tui.theme import theme_for


@dataclass(frozen=True)
class Choice:
    value: str
    label: str
    description: str = ""
    accent: str | None = None
    trailing: str = ""
    group: str = ""
    group_trailing: str = ""
    disabled: bool = False
    marker: str = ""
    edit_value: str | None = None
    edit_placeholder: str = ""


ChoiceSelectionHandler = Callable[[str], Awaitable[tuple[Choice, ...] | None]]
ChoiceCancelHandler = Callable[[], bool]
MultiChoiceConfirmationHandler = Callable[
    [tuple[str, ...]],
    Awaitable[tuple[Choice, ...] | None],
]
ChoiceEditHandler = Callable[[str, str], Awaitable[tuple[Choice, ...] | None]]
ScreenResult = TypeVar("ScreenResult")
MenuDestination = Literal["actions", "plugins", "sessions"]


@dataclass(frozen=True)
class _ChoicePage:
    navigation_id: MenuDestination | None
    retain_for_switching: bool
    title: str
    placeholder: str
    choices: tuple[Choice, ...]
    initial_value: str | None
    filter_value: str
    on_highlight: Callable[[str], None] | None
    on_select: ChoiceSelectionHandler | None
    on_cancel: ChoiceCancelHandler | None
    retain_focus_position_on_select: bool


class _ChoiceGroupHeader(Table):
    """Marks a table-backed group heading without conflating it with disabled rows."""


class _ChoiceGroupHeaderText(Text):
    """Marks a text-backed group heading without conflating it with disabled rows."""


def grouped_choice_options(
    choices: tuple[Choice, ...],
    *,
    group_accent: str = "#7d8794",
) -> list[Option]:
    """Render choices with the shared TUI group headers and row treatment."""

    options: list[Option] = []
    previous_group: object = object()
    for choice in choices:
        if choice.group != previous_group:
            if choice.group:
                if isinstance(previous_group, str) and previous_group:
                    options.append(Option(Text(" "), disabled=True))
                group_header: Text | Table
                if choice.group_trailing:
                    row = _ChoiceGroupHeader.grid(expand=True, padding=0)
                    row.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
                    row.add_column(justify="right", no_wrap=True)
                    row.add_row(
                        Text(choice.group, style=f"bold {group_accent}"),
                        Text(choice.group_trailing, style="#7d8794"),
                    )
                    group_header = row
                else:
                    group_header = _ChoiceGroupHeaderText(
                        choice.group,
                        style=f"bold {group_accent}",
                        overflow="ellipsis",
                        no_wrap=True,
                    )
                options.append(Option(group_header, disabled=True))
            previous_group = choice.group
        label = Text(overflow="ellipsis", no_wrap=True)
        if choice.marker:
            label.append(f"{choice.marker} ", style=f"bold {group_accent}")
        label.append(choice.label, style=choice.accent or "")
        if choice.trailing:
            row = Table.grid(expand=True, padding=0)
            row.add_column(ratio=2, overflow="ellipsis", no_wrap=True)
            row.add_column(ratio=1)
            row.add_column(width=2)
            row.add_column(justify="right", no_wrap=True)
            row.add_row(label, "", "", Text(choice.trailing, style="#7d8794"))
            options.append(Option(row, id=choice.value, disabled=choice.disabled))
            continue
        if choice.description:
            label.append(f"\n{choice.description}", style="#7d8794")
        options.append(Option(label, id=choice.value, disabled=choice.disabled))
    return options


class ChoiceScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("up", "previous_choice", "Previous", show=False, priority=True),
        Binding("down", "next_choice", "Next", show=False, priority=True),
        Binding("left", "previous_group", "Previous group", show=False, priority=True),
        Binding("right", "next_group", "Next group", show=False, priority=True),
        Binding("enter", "accept_choice", "Select", show=False, priority=True),
        Binding("escape", "cancel", "Close", show=False),
    ]

    def __init__(
        self,
        *,
        title: str,
        placeholder: str,
        choices: tuple[Choice, ...],
        initial_value: str | None = None,
        on_highlight: Callable[[str], None] | None = None,
        on_select: ChoiceSelectionHandler | None = None,
        on_cancel: ChoiceCancelHandler | None = None,
        retain_focus_position_on_select: bool = False,
        navigation_id: MenuDestination | None = None,
        retain_for_switching: bool = False,
    ) -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._choices = choices
        self._initial_value = initial_value
        self._on_highlight = on_highlight
        self._on_select = on_select
        self._on_cancel = on_cancel
        self._retain_focus_position_on_select = retain_focus_position_on_select
        self._navigation_id = navigation_id
        self._retain_for_switching = retain_for_switching
        self._page_stack: list[_ChoicePage] = []
        self._child_background: Color | None = None

    @property
    def navigation_id(self) -> MenuDestination | None:
        """Identify the current top-level keyboard navigation destination."""

        return self._navigation_id

    @property
    def navigation_root_id(self) -> MenuDestination | None:
        """Identify the root that owns this menu's in-place page history."""

        return self._page_stack[0].navigation_id if self._page_stack else self._navigation_id

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-panel"):
            yield Static(self._title, id="choice-title")
            yield self._filter_input()
            yield OptionList(
                *self._options(self._choices, group_accent=theme_for(self).accent),
                id="choice-list",
                classes="grouped-menu-list",
            )
            yield Static(
                "↑/↓ move · ←/→ group · Enter select · Esc cancel",
                id="choice-hint",
            )

    def _filter_input(self) -> Input:
        return TuiInput(placeholder=self._placeholder, id="choice-filter")

    def on_mount(self) -> None:
        self.query_one("#choice-filter", Input).focus()
        options = self.query_one("#choice-list", OptionList)
        if options.option_count:
            options.highlighted = self._highlight_index(
                options,
                preferred_value=self._initial_value,
            )

    def replace_page(
        self,
        *,
        title: str,
        placeholder: str,
        choices: tuple[Choice, ...],
        initial_value: str | None = None,
        on_highlight: Callable[[str], None] | None = None,
        on_select: ChoiceSelectionHandler | None = None,
        on_cancel: ChoiceCancelHandler | None = None,
        retain_focus_position_on_select: bool = False,
        navigation_id: MenuDestination | None = None,
        retain_for_switching: bool = False,
    ) -> None:
        """Replace one menu level without stacking another modal screen."""

        self._title = title
        self._placeholder = placeholder
        self._choices = choices
        self._on_highlight = on_highlight
        self._on_select = on_select
        self._on_cancel = on_cancel
        self._retain_focus_position_on_select = retain_focus_position_on_select
        self._navigation_id = navigation_id
        self._retain_for_switching = retain_for_switching
        self.query_one("#choice-title", Static).update(title)
        filter_input = self.query_one("#choice-filter", Input)
        filter_input.placeholder = placeholder
        filter_input.value = ""
        self._refresh_options(needle="", preferred_value=initial_value)
        filter_input.focus()

    def switch_page(
        self,
        *,
        navigation_id: MenuDestination,
        title: str,
        placeholder: str,
        choices: tuple[Choice, ...],
        initial_value: str | None = None,
        on_highlight: Callable[[str], None] | None = None,
        on_select: ChoiceSelectionHandler | None = None,
        on_cancel: ChoiceCancelHandler | None = None,
        retain_focus_position_on_select: bool = False,
        retain_for_switching: bool = False,
    ) -> None:
        """Switch keyboard menu destinations without creating another modal.

        A retained root remains the single Esc destination while peer roots
        replace their complete page history. Repeating the shortcut for the
        visible destination is intentionally idempotent.
        """

        if self._navigation_id == navigation_id:
            return
        root = self._page_stack[0] if self._page_stack else self._page()
        returning_to_root = root.retain_for_switching and navigation_id == root.navigation_id
        self._page_stack = [root] if root.retain_for_switching and not returning_to_root else []
        self.replace_page(
            navigation_id=navigation_id,
            title=title,
            placeholder=placeholder,
            choices=choices,
            initial_value=initial_value,
            on_highlight=on_highlight,
            on_select=on_select,
            on_cancel=on_cancel,
            retain_focus_position_on_select=retain_focus_position_on_select,
            retain_for_switching=(
                root.retain_for_switching if returning_to_root else retain_for_switching
            ),
        )

    def push_page(
        self,
        *,
        title: str,
        placeholder: str,
        choices: tuple[Choice, ...],
        initial_value: str | None = None,
        on_highlight: Callable[[str], None] | None = None,
        on_select: ChoiceSelectionHandler | None = None,
        on_cancel: ChoiceCancelHandler | None = None,
        retain_focus_position_on_select: bool = False,
        navigation_id: MenuDestination | None = None,
        retain_for_switching: bool = False,
    ) -> None:
        """Navigate to a grouped submenu while retaining the current page for Esc."""

        self._page_stack.append(self._page())
        self.replace_page(
            title=title,
            placeholder=placeholder,
            choices=choices,
            initial_value=initial_value,
            on_highlight=on_highlight,
            on_select=on_select,
            on_cancel=on_cancel,
            retain_focus_position_on_select=retain_focus_position_on_select,
            navigation_id=navigation_id,
            retain_for_switching=retain_for_switching,
        )

    def pop_page(
        self,
        *,
        choices: tuple[Choice, ...] | None = None,
        initial_value: str | None = None,
    ) -> bool:
        """Return to the previous grouped menu page, if one exists."""

        if not self._page_stack:
            return False
        page = self._page_stack.pop()
        self.replace_page(
            title=page.title,
            placeholder=page.placeholder,
            choices=choices if choices is not None else page.choices,
            initial_value=initial_value if initial_value is not None else page.initial_value,
            on_highlight=page.on_highlight,
            on_select=page.on_select,
            on_cancel=page.on_cancel,
            retain_focus_position_on_select=page.retain_focus_position_on_select,
            navigation_id=page.navigation_id,
            retain_for_switching=page.retain_for_switching,
        )
        if choices is None and initial_value is None and page.filter_value:
            self.query_one("#choice-filter", Input).value = page.filter_value
        return True

    def update_parent_choices(
        self,
        choices: tuple[Choice, ...],
        *,
        initial_value: str | None = None,
    ) -> bool:
        """Refresh the immediate parent snapshot retained for backward navigation."""

        if not self._page_stack:
            return False
        parent = self._page_stack[-1]
        self._page_stack[-1] = replace(
            parent,
            choices=choices,
            initial_value=initial_value if initial_value is not None else parent.initial_value,
        )
        return True

    async def open_child(self, screen: ModalScreen[ScreenResult]) -> ScreenResult | None:
        """Replace this menu visually with a child and restore it on return.

        The parent remains on the screen stack so its filter, selection, and page
        history stay intact, but it must not remain visible behind the child.  A
        visible parent turns ordinary submenu navigation into nested modal chrome
        and compounds Textual's backdrop on every level.
        """

        async def wait_for_child() -> ScreenResult | None:
            self._hide_for_child()
            return await self.app.push_screen_wait(screen)

        worker = self.app.run_worker(wait_for_child(), group="choice-child")
        return await worker.wait()

    def show_child(self, screen: ModalScreen[None]) -> None:
        """Show a result-free child without creating a waiting worker."""

        self._hide_for_child()
        self.app.push_screen(screen)

    def _hide_for_child(self) -> None:
        self.query_one("#choice-panel", Vertical).display = False
        self._child_background = self.styles.background
        self.styles.background = Color(0, 0, 0, 0)

    def on_screen_resume(self) -> None:
        """Restore a parent menu only after its child has left the screen stack."""

        background = self._child_background
        if background is None:
            return
        self._child_background = None
        self.styles.background = background
        self.query_one("#choice-panel", Vertical).display = True
        self.call_after_refresh(self.query_one("#choice-filter", Input).focus)

    def _page(self) -> _ChoicePage:
        return _ChoicePage(
            navigation_id=self._navigation_id,
            retain_for_switching=self._retain_for_switching,
            title=self._title,
            placeholder=self._placeholder,
            choices=self._choices,
            initial_value=self._selected_value(),
            filter_value=self.query_one("#choice-filter", Input).value,
            on_highlight=self._on_highlight,
            on_select=self._on_select,
            on_cancel=self._on_cancel,
            retain_focus_position_on_select=self._retain_focus_position_on_select,
        )

    def _selected_value(self) -> str | None:
        options = self.query_one("#choice-list", OptionList)
        highlighted = options.highlighted
        if highlighted is None:
            return None
        option = options.get_option_at_index(highlighted)
        return str(option.id) if option.id is not None else None

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh_options(needle=event.value, preferred_value=None)

    def _refresh_options(
        self,
        *,
        needle: str,
        preferred_value: str | None,
        preferred_position: int | None = None,
    ) -> None:
        normalized_needle = needle.casefold().strip()
        choices = tuple(
            choice for choice in self._choices if self._matches_filter(choice, normalized_needle)
        )
        options = self.query_one("#choice-list", OptionList)
        options.clear_options()
        options.add_options(self._options(choices, group_accent=theme_for(self).accent))
        options.highlighted = (
            self._highlight_index(
                options,
                preferred_value=preferred_value,
                preferred_position=preferred_position,
            )
            if choices
            else None
        )

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        options = self.query_one("#choice-list", OptionList)
        if options.option_count and options.highlighted is None:
            options.highlighted = self._highlight_index(options)
        options.focus()

    async def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        if event.option.id is None:
            return
        await self._select_choice(str(event.option.id))

    async def _select_choice(self, selected: str) -> None:
        if self._on_select is None:
            self.dismiss(selected)
            return
        focus_position = self._highlight_position()
        choices = await self._on_select(selected)
        if choices is None:
            return
        self._choices = choices
        self._refresh_options(
            needle=self.query_one("#choice-filter", Input).value,
            preferred_value=(None if self._retain_focus_position_on_select else selected),
            preferred_position=(focus_position if self._retain_focus_position_on_select else None),
        )

    def _highlight_position(self) -> int | None:
        options = self.query_one("#choice-list", OptionList)
        highlighted = options.highlighted
        if highlighted is None:
            return None
        return sum(not options.get_option_at_index(index).disabled for index in range(highlighted))

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self._on_highlight is not None and event.option.id is not None:
            self._on_highlight(str(event.option.id))

    def action_cancel(self) -> None:
        if self._on_cancel is not None and self._on_cancel():
            return
        if self.pop_page():
            return
        self.dismiss(None)

    def action_previous_choice(self) -> None:
        self._move_choice(-1)

    def action_next_choice(self) -> None:
        self._move_choice(1)

    def _move_choice(self, direction: int) -> None:
        options = self.query_one("#choice-list", OptionList)
        previous = options.highlighted
        if direction < 0:
            options.action_cursor_up()
        else:
            options.action_cursor_down()
        self._reveal_group_header_after_upward_move(options, previous)

    def action_previous_group(self) -> None:
        self._move_group(-1)

    def action_next_group(self) -> None:
        self._move_group(1)

    def _move_group(self, direction: int) -> None:
        options = self.query_one("#choice-list", OptionList)
        starts = tuple(
            index + 1
            for index in range(options.option_count - 1)
            if self._is_group_header(options.get_option_at_index(index))
            and not options.get_option_at_index(index + 1).disabled
        )
        if not starts:
            return
        highlighted = options.highlighted
        if highlighted is None:
            options.highlighted = starts[0]
            return
        current_group = max(
            (index for index, start in enumerate(starts) if start <= highlighted),
            default=0,
        )
        options.highlighted = starts[(current_group + direction) % len(starts)]
        self._reveal_group_header_after_upward_move(options, highlighted)

    @classmethod
    def _reveal_group_header_after_upward_move(
        cls,
        options: OptionList,
        previous: int | None,
    ) -> None:
        highlighted = options.highlighted
        if (
            previous is None
            or highlighted is None
            or highlighted >= previous
            or highlighted == 0
            or not cls._is_group_header(options.get_option_at_index(highlighted - 1))
        ):
            return
        # OptionList has already made the selected row visible. Moving upward
        # can leave its one-line group header just above the viewport; reveal
        # that context without changing downward navigation's valid scroll.
        options.scroll_relative(
            y=-1,
            animate=False,
            force=True,
            immediate=True,
        )

    @staticmethod
    def _is_group_header(option: Option) -> bool:
        return option.disabled and isinstance(
            option.prompt,
            (_ChoiceGroupHeader, _ChoiceGroupHeaderText),
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"previous_group", "next_group"}:
            return any(choice.group for choice in self._choices)
        return super().check_action(action, parameters)

    def action_accept_choice(self) -> None:
        self.query_one("#choice-list", OptionList).action_select()

    @staticmethod
    def _options(
        choices: tuple[Choice, ...],
        *,
        group_accent: str = "#7d8794",
    ) -> list[Option]:
        return grouped_choice_options(choices, group_accent=group_accent)

    @staticmethod
    def _highlight_index(
        options: OptionList,
        *,
        preferred_value: str | None = None,
        preferred_position: int | None = None,
    ) -> int | None:
        enabled: list[int] = []
        for index in range(options.option_count):
            option = options.get_option_at_index(index)
            if option.disabled:
                continue
            enabled.append(index)
            if preferred_value is not None and option.id == preferred_value:
                return index
        if not enabled:
            return None
        if preferred_position is not None:
            return enabled[min(preferred_position, len(enabled) - 1)]
        return enabled[0]

    @staticmethod
    def _matches_filter(choice: Choice, normalized_needle: str) -> bool:
        return (
            not normalized_needle
            or normalized_needle in choice.label.casefold()
            or normalized_needle in choice.description.casefold()
            or normalized_needle in choice.group.casefold()
            or normalized_needle in choice.trailing.casefold()
        )


class _MultiChoiceFilterInput(TuiInput):
    BINDINGS = [
        Binding("space", "toggle_choice", "Toggle", show=False, priority=True),
    ]

    def action_toggle_choice(self) -> None:
        screen = self.screen
        if isinstance(screen, MultiChoiceScreen):
            screen.action_toggle_choice()


class _EditableChoiceList(OptionList):
    """Keep right-click reserved for the screen's inline-editor paste action."""

    async def _on_click(self, event: events.Click) -> None:
        if event.button == 3:
            event.prevent_default()
            event.stop()
            return
        await super()._on_click(event)


class EditableChoiceScreen(ChoiceScreen):
    """Edit the selected choice label in place without opening another screen."""

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
    ) -> None:
        super().__init__(title=title, placeholder=placeholder, choices=choices)
        self._on_save = on_save
        self._editing_value: str | None = None
        self._edit_text = ""
        self._edit_cursor = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-panel"):
            yield Static(self._title, id="choice-title")
            yield self._filter_input()
            yield _EditableChoiceList(
                *self._options(self._choices, group_accent=theme_for(self).accent),
                id="choice-list",
                classes="grouped-menu-list",
            )
            yield Static(
                "↑/↓ move · ←/→ group · Enter edit · Esc cancel",
                id="choice-hint",
            )

    async def _select_choice(self, selected: str) -> None:
        if self._editing_value is None:
            self._begin_edit(selected)
            return
        if selected != self._editing_value:
            return
        choices = await self._on_save(selected, self._edit_text)
        if choices is not None:
            self._finish_edit(choices, selected=selected)

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
        self._insert_text(" ".join(event.text.splitlines()))

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
        self._render_editor()

    def action_next_group(self) -> None:
        if self._editing_value is None:
            super().action_next_group()
            return
        self._edit_cursor = min(len(self._edit_text), self._edit_cursor + 1)
        self._render_editor()

    def action_edit_home(self) -> None:
        if self._editing_value is not None:
            self._edit_cursor = 0
            self._render_editor()

    def action_edit_end(self) -> None:
        if self._editing_value is not None:
            self._edit_cursor = len(self._edit_text)
            self._render_editor()

    def action_edit_clear(self) -> None:
        if self._editing_value is not None:
            self._edit_text = ""
            self._edit_cursor = 0
            self._render_editor()

    def action_edit_backspace(self) -> None:
        if self._editing_value is None or self._edit_cursor == 0:
            return
        self._edit_text = (
            self._edit_text[: self._edit_cursor - 1] + self._edit_text[self._edit_cursor :]
        )
        self._edit_cursor -= 1
        self._render_editor()

    def action_edit_delete(self) -> None:
        if self._editing_value is None or self._edit_cursor >= len(self._edit_text):
            return
        self._edit_text = (
            self._edit_text[: self._edit_cursor] + self._edit_text[self._edit_cursor + 1 :]
        )
        self._render_editor()

    def action_cancel(self) -> None:
        if self._editing_value is None:
            super().action_cancel()
            return
        selected = self._editing_value
        self._finish_edit(self._choices, selected=selected)

    def _begin_edit(self, selected: str) -> None:
        choice = next((choice for choice in self._choices if choice.value == selected), None)
        if choice is None or choice.disabled:
            return
        self._editing_value = selected
        self._edit_text = choice.edit_value if choice.edit_value is not None else choice.label
        self._edit_cursor = len(self._edit_text)
        self.query_one("#choice-filter", Input).disabled = True
        self.query_one("#choice-hint", Static).update(
            "Type edit · ←/→ move · Enter save · Esc cancel"
        )
        self._render_editor()

    def _finish_edit(self, choices: tuple[Choice, ...], *, selected: str) -> None:
        self._editing_value = None
        self._edit_text = ""
        self._edit_cursor = 0
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
        self.query_one("#choice-hint", Static).update(
            "↑/↓ move · ←/→ group · Enter edit · Esc cancel"
        )
        self._refresh_options(needle=needle, preferred_value=selected)
        self.query_one("#choice-list", OptionList).focus()

    def _replace_choices(self, choices: tuple[Choice, ...], *, selected: str) -> None:
        """Replace the live menu projection while preserving filter and focus."""

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
        accent = theme_for(self).accent
        display_text = self._editor_display_text(self._edit_text)
        prompt = Text(overflow="ellipsis", no_wrap=True)
        if not display_text:
            prompt.append(" ", style=f"not bold reverse {accent}")
            choice = next(
                (choice for choice in self._choices if choice.value == editing_value),
                None,
            )
            if choice is not None and choice.edit_placeholder:
                prompt.append(choice.edit_placeholder, style="not bold #7d8794 italic")
            options.replace_option_prompt_at_index(option_index, prompt)
            return
        prompt.append(display_text[: self._edit_cursor], style="not bold")
        if self._edit_cursor < len(display_text):
            prompt.append(
                display_text[self._edit_cursor],
                style=f"not bold reverse {accent}",
            )
            prompt.append(display_text[self._edit_cursor + 1 :], style="not bold")
        else:
            prompt.append(" ", style=f"not bold reverse {accent}")
        options.replace_option_prompt_at_index(option_index, prompt)

    def _editor_display_text(self, value: str) -> str:
        """Return the visible editor text; subclasses may mask sensitive values."""

        return value


class MultiChoiceScreen(ChoiceScreen):
    """Keep a filtered selection set while Enter confirms one or many choices."""

    BINDINGS = [
        Binding("up", "previous_choice", "Previous", show=False, priority=True),
        Binding("down", "next_choice", "Next", show=False, priority=True),
        Binding("left", "previous_group", "Previous group", show=False, priority=True),
        Binding("right", "next_group", "Next group", show=False, priority=True),
        Binding("space", "toggle_choice", "Toggle", show=False, priority=True),
        Binding("enter", "accept_choice", "Confirm", show=False, priority=True),
        Binding("escape", "cancel", "Close", show=False),
    ]

    def __init__(
        self,
        *,
        title: str,
        placeholder: str,
        choices: tuple[Choice, ...],
        on_confirm: MultiChoiceConfirmationHandler,
        selected_marker: str,
        confirm_label: str = "Confirm",
    ) -> None:
        super().__init__(title=title, placeholder=placeholder, choices=choices)
        self._on_confirm = on_confirm
        self._selected: set[str] = set()
        self._selected_marker = selected_marker
        self._confirm_label = confirm_label
        self._confirmation_values: tuple[str, ...] = ()
        self._catalog_choices: tuple[Choice, ...] = ()
        self._catalog_filter = ""

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#choice-hint", Static).update(
            "↑/↓ move · Space select · Enter confirm · Esc cancel"
        )

    def _filter_input(self) -> Input:
        return _MultiChoiceFilterInput(
            placeholder=self._placeholder,
            id="choice-filter",
        )

    def action_toggle_choice(self) -> None:
        if self._confirmation_values:
            return
        selected = self._highlighted_value()
        if selected is None:
            return
        if selected in self._selected:
            self._selected.remove(selected)
        else:
            self._selected.add(selected)
        self._choices = self._project_markers(self._choices)
        self._refresh_options(
            needle=self.query_one("#choice-filter", Input).value,
            preferred_value=selected,
        )

    async def _select_choice(self, highlighted: str) -> None:
        if self._confirmation_values:
            if highlighted == "__multi_cancel__":
                self._leave_confirmation(self._catalog_choices)
                return
            if highlighted != "__multi_confirm__":
                return
            choices = await self._on_confirm(self._confirmation_values)
            self._leave_confirmation(self._catalog_choices if choices is None else choices)
            return
        selected = tuple(
            choice.value for choice in self._choices if choice.value in self._selected
        ) or (highlighted,)
        self._enter_confirmation(selected)

    def action_cancel(self) -> None:
        if self._confirmation_values:
            self._leave_confirmation(self._catalog_choices)
            return
        super().action_cancel()

    def _enter_confirmation(self, selected: tuple[str, ...]) -> None:
        self._confirmation_values = selected
        self._catalog_choices = self._choices
        filter_input = self.query_one("#choice-filter", Input)
        self._catalog_filter = filter_input.value
        self._choices = (
            Choice("__multi_cancel__", "Cancel"),
            Choice(
                "__multi_confirm__",
                f"{self._confirm_label} {len(selected)} selected",
            ),
        )
        self.query_one("#choice-title", Static).update(f"CONFIRM {self._title}")
        self.query_one("#choice-hint", Static).update("↑/↓ choose · Enter select · Esc back")
        filter_input.value = ""
        self._refresh_options(needle="", preferred_value="__multi_cancel__")

    def _leave_confirmation(self, choices: tuple[Choice, ...]) -> None:
        self._confirmation_values = ()
        remaining = {choice.value for choice in choices}
        self._selected.intersection_update(remaining)
        self._choices = self._project_markers(choices)
        self.query_one("#choice-title", Static).update(self._title)
        self.query_one("#choice-hint", Static).update(
            "↑/↓ move · Space select · Enter confirm · Esc cancel"
        )
        filter_input = self.query_one("#choice-filter", Input)
        filter_input.value = self._catalog_filter
        self._refresh_options(
            needle=self._catalog_filter,
            preferred_value=None,
        )
        self._catalog_choices = ()
        self._catalog_filter = ""

    def _highlighted_value(self) -> str | None:
        options = self.query_one("#choice-list", OptionList)
        highlighted = options.highlighted
        if highlighted is None:
            return None
        option = options.get_option_at_index(highlighted)
        return str(option.id) if option.id is not None and not option.disabled else None

    def _project_markers(self, choices: tuple[Choice, ...]) -> tuple[Choice, ...]:
        return tuple(
            replace(
                choice,
                marker=(self._selected_marker if choice.value in self._selected else ""),
            )
            for choice in choices
        )


class ConfirmationScreen(ChoiceScreen):
    """Present a destructive confirmation with the shared menu chrome."""

    CONFIRM_VALUE = "__confirmation_confirm__"

    def __init__(
        self,
        *,
        title: str,
        message: str,
        confirm_label: str = "Confirm",
    ) -> None:
        super().__init__(
            title=f"CONFIRM {title}",
            placeholder="Choose an action…",
            choices=(
                Choice("__confirmation_cancel__", "Cancel"),
                Choice(self.CONFIRM_VALUE, confirm_label, description=message),
            ),
            initial_value="__confirmation_cancel__",
        )

    def on_mount(self) -> None:
        super().on_mount()
        self.call_after_refresh(self.query_one("#choice-list", OptionList).focus)

    @classmethod
    def confirmed(cls, selected: str | None) -> bool:
        return selected == cls.CONFIRM_VALUE


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close", show=False, priority=True),
    ]

    def compose(self) -> ComposeResult:
        # Build the panel from a Text so padding tracks visible width, not
        # markup length — keeping the shortcut columns aligned in the flat panel.
        text = Text()
        accent = theme_for(self).accent
        text.append("OURO shortcuts\n\n", style=f"bold {accent}")
        pairs = (
            ("Enter", "send / queue", "Shift+Enter", "new line"),
            ("Ctrl+Enter / Ctrl+J", "guide active run", "↑ / ↓", "input history"),
            ("Ctrl+N", "new session", "Ctrl+S", "sessions"),
            ("Alt+P", "plugins", "Ctrl+P", "actions"),
            ("Ctrl+H", "help", "Ctrl+↑", "conversation top"),
            ("Ctrl+↓", "conversation bottom", "Ctrl+←", "previous user message"),
            ("Ctrl+→", "next user message", "Ctrl+C", "abort run"),
            ("Ctrl+Q", "quit", "Esc", "close menu"),
        )
        for left_key, left_desc, right_key, right_desc in pairs:
            text.append(left_key, style="bold")
            text.append(f" {left_desc}")
            text.append(" " * max(0, 40 - len(left_key) - len(left_desc) - 1))
            text.append(right_key, style="bold")
            text.append(f" {right_desc}\n")
        text.append("\nBuilt-in slash commands\n\n", style=f"bold {accent}")
        text.append(slash_command_help(), style="#aeb5bf")
        text.append(
            "\n\nType / to search built-in and active plugin commands. Unique plugin "
            "commands use the short form; ambiguous names use /plugin.id:command.",
            style="#8f9aa8",
        )
        yield Static(text, id="help-panel")

    def action_close(self) -> None:
        self.dismiss(None)
