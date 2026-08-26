from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Literal, TypeVar

from rich.cells import cell_len
from rich.table import Table
from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ouro.tui.clipboard import ClipboardReadError, read_system_clipboard
from ouro.tui.commands import TUI_COMMANDS, TuiCommand
from ouro.tui.text_inputs import TuiInput
from ouro.tui.theme import TUI_DESIGN, theme_for


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
    selection_marker: str = ""
    edit_value: str | None = None
    edit_placeholder: str = ""
    loading: bool = False


_HELP_SHORTCUTS = (
    ("Enter", "send / queue", "Shift+Enter", "new line"),
    ("Ctrl+Enter / Ctrl+J", "guide active run", "↑ / ↓", "input history"),
    ("Ctrl+N", "new session", "Ctrl+S", "sessions"),
    ("Alt+T", "cycle model thinking", "Alt+A", "cycle session approval"),
    ("Alt+P", "plugins", "Ctrl+P", "actions"),
    ("Ctrl+H", "help", "Ctrl+↑", "conversation top"),
    ("Ctrl+↓", "conversation bottom", "Ctrl+←", "previous user message"),
    ("Ctrl+→", "next user message", "Ctrl+C", "abort run"),
    ("Ctrl+Q", "quit", "Esc", "close menu"),
)


def help_choices(
    *,
    plugin_commands: tuple[TuiCommand, ...] = (),
) -> tuple[Choice, ...]:
    """Project help onto the shared searchable menu surface."""

    shortcuts = tuple(
        Choice(
            f"shortcut:{row}:{side}",
            key,
            trailing=description,
            group="SHORTCUTS",
        )
        for row, pair in enumerate(_HELP_SHORTCUTS)
        for side, (key, description) in enumerate((pair[:2], pair[2:]))
    )
    commands = tuple(
        Choice(
            f"command:{command.name}",
            command.usage,
            trailing=command.description,
            group="BUILT-IN SLASH COMMANDS",
        )
        for command in TUI_COMMANDS
    )
    plugins = tuple(
        Choice(
            f"plugin-command:{command.name}",
            command.usage,
            trailing=command.title,
            group="PLUGIN SLASH COMMANDS",
        )
        for command in plugin_commands
    )
    return (*shortcuts, *commands, *plugins)


ChoiceSelectionHandler = Callable[[str], Awaitable[tuple[Choice, ...] | None]]
ChoiceRefreshHandler = Callable[[], Awaitable[tuple[Choice, ...] | None]]
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
    on_refresh: ChoiceRefreshHandler | None
    on_cancel: ChoiceCancelHandler | None
    retain_focus_position_on_select: bool
    interaction_hint: str | None


class _ChoiceGroupHeader(Table):
    """Marks a table-backed group heading without conflating it with disabled rows."""


class _ChoiceGroupHeaderText(Text):
    """Marks a text-backed group heading without conflating it with disabled rows."""


def _choice_option(
    choice: Choice,
    *,
    group_accent: str,
    spinner_frame: int = 0,
) -> Option:
    """Render one selectable row independently from its surrounding group chrome."""

    label = Text(overflow="ellipsis", no_wrap=True)
    marker = TUI_DESIGN.spinner_frames[spinner_frame] if choice.loading else choice.marker
    markers = tuple(value for value in (choice.selection_marker, marker) if value)
    if markers:
        label.append(f"{' '.join(markers)} ", style=f"bold {group_accent}")
    label.append(choice.label, style=choice.accent or "")
    if choice.trailing:
        row = Table.grid(expand=True, padding=0)
        row.add_column(ratio=2, overflow="ellipsis", no_wrap=True)
        row.add_column(ratio=1)
        row.add_column(width=2)
        row.add_column(justify="right", no_wrap=True)
        row.add_row(label, "", "", Text(choice.trailing, style=TUI_DESIGN.text_choice))
        return Option(row, id=choice.value, disabled=choice.disabled)
    if choice.description:
        label.append(f"\n{choice.description}", style=TUI_DESIGN.text_choice)
    return Option(label, id=choice.value, disabled=choice.disabled)


def grouped_choice_options(
    choices: tuple[Choice, ...],
    *,
    group_accent: str = TUI_DESIGN.text_choice,
    spinner_frame: int = 0,
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
                        Text(choice.group_trailing, style=TUI_DESIGN.text_choice),
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
        options.append(
            _choice_option(
                choice,
                group_accent=group_accent,
                spinner_frame=spinner_frame,
            )
        )
    return options


class ChoiceScreen(ModalScreen[str | None]):
    MIN_LIST_HEIGHT = 5
    MAX_LIST_HEIGHT = 20
    # Fixed title, filter, and hint rows surrounding the scrollable list.
    MENU_CHROME_HEIGHT = 7
    DEFAULT_HINT = TUI_DESIGN.inline_separator.join(
        ("↑/↓ move", "←/→ group", "Enter select", "Esc close")
    )
    BACK_HINT = TUI_DESIGN.inline_separator.join(
        ("↑/↓ move", "←/→ group", "Enter select", "Esc back")
    )

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
        on_refresh: ChoiceRefreshHandler | None = None,
        on_cancel: ChoiceCancelHandler | None = None,
        retain_focus_position_on_select: bool = False,
        navigation_id: MenuDestination | None = None,
        retain_for_switching: bool = False,
        interaction_hint: str | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._placeholder = placeholder
        self._choices = choices
        self._initial_value = initial_value
        self._on_highlight = on_highlight
        self._on_select = on_select
        self._on_refresh = on_refresh
        self._on_cancel = on_cancel
        self._retain_focus_position_on_select = retain_focus_position_on_select
        self._navigation_id = navigation_id
        self._retain_for_switching = retain_for_switching
        self._interaction_hint = interaction_hint
        self._page_stack: list[_ChoicePage] = []
        self._child_background: Color | None = None
        self._page_revision = 0
        self._spinner_frame = 0
        self._spinner_timer: Timer | None = None

    @property
    def navigation_id(self) -> MenuDestination | None:
        """Identify the current top-level keyboard navigation destination."""

        return self._navigation_id

    @property
    def navigation_root_id(self) -> MenuDestination | None:
        """Identify the root that owns this menu's in-place page history."""

        return self._page_stack[0].navigation_id if self._page_stack else self._navigation_id

    @property
    def page_revision(self) -> int:
        """Identify the visible page so background work cannot update a replacement page."""

        return self._page_revision

    @property
    def page_title(self) -> str:
        """Return the title of the page currently rendered on this menu surface."""

        return self._title

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-panel"):
            yield Static(self._title, id="choice-title")
            yield self._filter_input()
            yield self._choice_list()
            yield Static(self._resolved_hint(), id="choice-hint")

    def _filter_input(self) -> Input:
        return TuiInput(placeholder=self._placeholder, id="choice-filter")

    def _choice_list(self) -> OptionList:
        """Build the menu body; specialized screens can replace it without copying the frame."""

        return OptionList(
            *self._options(self._choices, group_accent=theme_for(self).accent),
            id="choice-list",
            classes="grouped-menu-list",
        )

    def _show_hint(self, hint: str | None = None) -> None:
        """Render the current interaction contract in the shared menu footer."""

        self.query_one("#choice-hint", Static).update(self._resolved_hint(hint))

    def _resolved_hint(self, hint: str | None = None) -> str:
        """Resolve page customization before the screen's navigation-aware default."""

        return (
            hint
            or self._interaction_hint
            or (self.BACK_HINT if self._page_stack else self.DEFAULT_HINT)
        )

    def on_mount(self) -> None:
        self._sync_loading_timer()
        self.query_one("#choice-filter", Input).focus()
        options = self.query_one("#choice-list", OptionList)
        if options.option_count:
            options.highlighted = self._highlight_index(
                options,
                preferred_value=self._initial_value,
            )

    def on_resize(self, event: events.Resize) -> None:
        """Keep the scrollable menu body within a bounded share of the terminal."""

        responsive_panel_height = event.size.height * 4 // 5
        list_height = responsive_panel_height - self.MENU_CHROME_HEIGHT
        self.query_one("#choice-list", OptionList).styles.max_height = max(
            self.MIN_LIST_HEIGHT,
            min(self.MAX_LIST_HEIGHT, list_height),
        )

    def on_unmount(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None

    def replace_page(
        self,
        *,
        title: str,
        placeholder: str,
        choices: tuple[Choice, ...],
        initial_value: str | None = None,
        filter_value: str = "",
        on_highlight: Callable[[str], None] | None = None,
        on_select: ChoiceSelectionHandler | None = None,
        on_refresh: ChoiceRefreshHandler | None = None,
        on_cancel: ChoiceCancelHandler | None = None,
        retain_focus_position_on_select: bool = False,
        navigation_id: MenuDestination | None = None,
        retain_for_switching: bool = False,
        interaction_hint: str | None = None,
    ) -> None:
        """Replace one menu level without stacking another modal screen."""

        self._page_revision += 1
        self._title = title
        self._placeholder = placeholder
        self._choices = choices
        self._on_highlight = on_highlight
        self._on_select = on_select
        self._on_refresh = on_refresh
        self._on_cancel = on_cancel
        self._retain_focus_position_on_select = retain_focus_position_on_select
        self._navigation_id = navigation_id
        self._retain_for_switching = retain_for_switching
        self._interaction_hint = interaction_hint
        self.query_one("#choice-title", Static).update(title)
        filter_input = self.query_one("#choice-filter", Input)
        filter_input.placeholder = placeholder
        with self.prevent(Input.Changed):
            filter_input.value = filter_value
        self._refresh_options(needle=filter_value, preferred_value=initial_value)
        self._show_hint(interaction_hint)
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
        on_refresh: ChoiceRefreshHandler | None = None,
        on_cancel: ChoiceCancelHandler | None = None,
        retain_focus_position_on_select: bool = False,
        retain_for_switching: bool = False,
        interaction_hint: str | None = None,
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
            on_refresh=on_refresh,
            on_cancel=on_cancel,
            retain_focus_position_on_select=retain_focus_position_on_select,
            retain_for_switching=(
                root.retain_for_switching if returning_to_root else retain_for_switching
            ),
            interaction_hint=interaction_hint,
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
        on_refresh: ChoiceRefreshHandler | None = None,
        on_cancel: ChoiceCancelHandler | None = None,
        retain_focus_position_on_select: bool = False,
        navigation_id: MenuDestination | None = None,
        retain_for_switching: bool = False,
        interaction_hint: str | None = None,
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
            on_refresh=on_refresh,
            on_cancel=on_cancel,
            retain_focus_position_on_select=retain_focus_position_on_select,
            navigation_id=navigation_id,
            retain_for_switching=retain_for_switching,
            interaction_hint=interaction_hint,
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
            filter_value=page.filter_value if initial_value is None else "",
            on_highlight=page.on_highlight,
            on_select=page.on_select,
            on_refresh=page.on_refresh,
            on_cancel=page.on_cancel,
            retain_focus_position_on_select=page.retain_focus_position_on_select,
            navigation_id=page.navigation_id,
            retain_for_switching=page.retain_for_switching,
            interaction_hint=page.interaction_hint,
        )
        return True

    async def pop_page_refreshed(self) -> bool:
        """Return to a parent page using its latest application projection."""

        if not self._page_stack:
            return False
        page = self._page_stack[-1]
        choices = await page.on_refresh() if page.on_refresh is not None else None
        return self.pop_page(choices=choices)

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

    def update_choices(
        self,
        choices: tuple[Choice, ...],
        *,
        expected_revision: int | None = None,
        preferred_value: str | None = None,
    ) -> bool:
        """Update the visible page when it still owns a background operation."""

        if (
            not self._accepts_choice_updates()
            or (expected_revision is not None and self._page_revision != expected_revision)
        ):
            return False
        self._choices = choices
        self._refresh_options(
            needle=self.query_one("#choice-filter", Input).value,
            preferred_value=preferred_value,
        )
        return True

    async def refresh_choices(self) -> bool:
        """Refresh whichever page is still visible after background work completes."""

        if not self._accepts_choice_updates() or self._on_refresh is None:
            return False
        page_revision = self._page_revision
        choices = await self._on_refresh()
        if choices is None:
            return False
        return self.update_choices(choices, expected_revision=page_revision)

    def _accepts_choice_updates(self) -> bool:
        """Return whether this menu still owns a live render surface."""

        return self.is_mounted and self.is_attached and self.is_running and self.display

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
            on_refresh=self._on_refresh,
            on_cancel=self._on_cancel,
            retain_focus_position_on_select=self._retain_focus_position_on_select,
            interaction_hint=self._interaction_hint,
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
        options.add_options(
            self._options(
                choices,
                group_accent=theme_for(self).accent,
                spinner_frame=self._spinner_frame,
            )
        )
        options.highlighted = (
            self._highlight_index(
                options,
                preferred_value=preferred_value,
                preferred_position=preferred_position,
            )
            if choices
            else None
        )
        self._sync_loading_timer()

    def _sync_loading_timer(self) -> None:
        if any(choice.loading for choice in self._choices):
            if self._spinner_timer is None:
                self._spinner_timer = self.set_interval(
                    0.12,
                    self._advance_loading_animation,
                )
        else:
            if self._spinner_timer is not None:
                self._spinner_timer.stop()
                self._spinner_timer = None
            self._spinner_frame = 0

    def _advance_loading_animation(self) -> None:
        self._spinner_frame = (self._spinner_frame + 1) % len(TUI_DESIGN.spinner_frames)
        loading = {choice.value: choice for choice in self._choices if choice.loading}
        if not loading:
            self._sync_loading_timer()
            return
        options = self.query_one("#choice-list", OptionList)
        accent = theme_for(self).accent
        for index in range(options.option_count):
            option = options.get_option_at_index(index)
            choice = loading.get(str(option.id)) if option.id is not None else None
            if choice is None:
                continue
            prompt = _choice_option(
                choice,
                group_accent=accent,
                spinner_frame=self._spinner_frame,
            ).prompt
            options.replace_option_prompt_at_index(index, prompt)

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
        page_revision = self._page_revision
        focus_position = self._highlight_position()
        choices = await self._on_select(selected)
        if (
            choices is None
            and self._page_revision == page_revision
            and self.is_mounted
            # Navigation roots dispatch the destination asynchronously. Redrawing
            # them here would briefly render the old page before that page arrives.
            and self._navigation_id is None
            and self._on_refresh is not None
        ):
            choices = await self._on_refresh()
        if choices is None or choices is self._choices:
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

    async def action_cancel(self) -> None:
        if self._on_cancel is not None and self._on_cancel():
            return
        if await self.pop_page_refreshed():
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
        group_accent: str = TUI_DESIGN.text_choice,
        spinner_frame: int = 0,
    ) -> list[Option]:
        return grouped_choice_options(
            choices,
            group_accent=group_accent,
            spinner_frame=spinner_frame,
        )

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
    DEFAULT_HINT = TUI_DESIGN.inline_separator.join(
        ("↑/↓ move", "←/→ group", "Enter edit", "Esc close")
    )
    BACK_HINT = TUI_DESIGN.inline_separator.join(
        ("↑/↓ move", "←/→ group", "Enter edit", "Esc back")
    )
    EDIT_HINT = TUI_DESIGN.inline_separator.join(
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
    ) -> None:
        super().__init__(title=title, placeholder=placeholder, choices=choices)
        self._on_save = on_save
        self._editing_value: str | None = None
        self._edit_text = ""
        self._edit_original_text = ""
        self._edit_cursor = 0
        self._edit_view_start = 0
        self._edit_cursor_visible = True
        self._edit_cursor_timer: Timer | None = None

    def on_unmount(self) -> None:
        self._stop_edit_cursor()
        super().on_unmount()

    def on_resize(self, event: events.Resize) -> None:
        super().on_resize(event)
        if self._editing_value is not None:
            self.call_after_refresh(self._render_editor)

    def _choice_list(self) -> OptionList:
        return _EditableChoiceList(
            *self._options(self._choices, group_accent=theme_for(self).accent),
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
        accent = theme_for(self).accent
        display_text = self._editor_display_text(self._edit_text)
        prompt = Text(no_wrap=True)
        cursor_style = (
            f"not bold reverse {accent}" if self._edit_cursor_visible else "not bold"
        )
        if not display_text:
            prompt.append(" ", style=cursor_style)
            choice = next(
                (choice for choice in self._choices if choice.value == editing_value),
                None,
            )
            if choice is not None and choice.edit_placeholder:
                prompt.append(
                    choice.edit_placeholder,
                    style=f"not bold {TUI_DESIGN.text_choice} italic",
                )
            options.replace_option_prompt_at_index(option_index, prompt)
            return
        editor_width = max(
            1,
            options.scrollable_content_region.width
            - options.get_component_styles("option-list--option").padding.width,
        )
        view_start, view_end = self._editor_view_bounds(display_text, editor_width)
        if view_start:
            prompt.append("…", style=f"not bold {TUI_DESIGN.text_choice}")
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
            prompt.append("…", style=f"not bold {TUI_DESIGN.text_choice}")
        options.replace_option_prompt_at_index(option_index, prompt)

    def _editor_view_bounds(self, display_text: str, width: int) -> tuple[int, int]:
        """Return a stable single-line window that always contains the edit cursor."""

        cursor = min(self._edit_cursor, len(display_text))
        start = min(self._edit_view_start, cursor)
        end = cursor + 1 if cursor < len(display_text) else len(display_text)

        def view_width(candidate_start: int, candidate_end: int) -> int:
            return (
                (1 if candidate_start else 0)
                + cell_len(display_text[candidate_start:candidate_end])
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


class MultiChoiceScreen(ChoiceScreen):
    """Keep a filtered selection set while Enter confirms one or many choices."""

    DEFAULT_HINT = TUI_DESIGN.inline_separator.join(
        ("↑/↓ move", "←/→ group", "Space toggle", "Enter confirm", "Esc close")
    )
    BACK_HINT = TUI_DESIGN.inline_separator.join(
        ("↑/↓ move", "←/→ group", "Space toggle", "Enter confirm", "Esc back")
    )
    CONFIRM_HINT = TUI_DESIGN.inline_separator.join(("↑/↓ move", "Enter select", "Esc back"))

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
        unselected_marker: str = "",
        confirm_label: str = "Confirm",
    ) -> None:
        super().__init__(title=title, placeholder=placeholder, choices=choices)
        self._on_confirm = on_confirm
        self._selected: set[str] = set()
        self._selected_marker = selected_marker
        self._unselected_marker = unselected_marker
        self._confirm_label = confirm_label
        self._confirmation_values: tuple[str, ...] = ()
        self._catalog_choices: tuple[Choice, ...] = ()
        self._catalog_filter = ""
        self._choices = self._project_markers(self._choices)

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
        choice = next(choice for choice in self._choices if choice.value == selected)
        options = self.query_one("#choice-list", OptionList)
        option_index = options.get_option_index(selected)
        prompt = _choice_option(
            choice,
            group_accent=theme_for(self).accent,
        ).prompt
        options.replace_option_prompt_at_index(
            option_index,
            prompt,
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

    async def action_cancel(self) -> None:
        if self._confirmation_values:
            self._leave_confirmation(self._catalog_choices)
            return
        await super().action_cancel()

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
        self._show_hint(self.CONFIRM_HINT)
        filter_input.value = ""
        self._refresh_options(needle="", preferred_value="__multi_cancel__")

    def _leave_confirmation(self, choices: tuple[Choice, ...]) -> None:
        self._confirmation_values = ()
        remaining = {choice.value for choice in choices}
        self._selected.intersection_update(remaining)
        self._choices = self._project_markers(choices)
        self.query_one("#choice-title", Static).update(self._title)
        self._show_hint()
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
                selection_marker=(
                    self._selected_marker
                    if choice.value in self._selected
                    else self._unselected_marker
                ),
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


class HelpScreen(ChoiceScreen):
    """Expose root-level help through the same searchable menu as in-menu help."""

    DEFAULT_HINT = TUI_DESIGN.inline_separator.join(("↑/↓ move", "←/→ group", "Esc close"))
    BACK_HINT = TUI_DESIGN.inline_separator.join(("↑/↓ move", "←/→ group", "Esc back"))

    def __init__(self, *, plugin_commands: tuple[TuiCommand, ...] = ()) -> None:
        super().__init__(
            title="KEYBOARD HELP",
            placeholder="Filter shortcuts and commands…",
            choices=help_choices(plugin_commands=plugin_commands),
        )

    async def _select_choice(self, selected: str) -> None:
        # Help rows describe interactions; selecting one has no second meaning.
        return None
