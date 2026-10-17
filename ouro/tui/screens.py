from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import partial
from typing import Literal, TypeVar, cast

from rich.cells import get_character_cell_size
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Vertical
from textual.css.styles import RulesMap
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.visual import RichVisual, Visual
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerCancelled

from ouro.tui.clipboard import ClipboardReadError, read_system_clipboard
from ouro.tui.commands import REFERENCE_COMMANDS, TUI_COMMANDS, TuiCommand
from ouro.tui.text_inputs import TuiInput
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, theme_for


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
    description_accent: str | None = None
    destructive: bool = False
    edit_lines: int = 1
    leading_swatch: str | None = None
    swatches: tuple[str, ...] = ()
    swatch_marker_index: int | None = None
    trailing_accent: str | None = None
    trailing_swatch: str | None = None
    trailing_badge: str = ""


_HELP_SHORTCUTS = (
    ("Enter", "send / queue / confirm", "Ctrl+Enter", "new line"),
    ("Ctrl+G", "guide current task", "↑ / ↓", "cursor / history when empty"),
    ("Ctrl+N", "new session", "Ctrl+O", "sessions"),
    ("Ctrl+T", "tasks", "Ctrl+S", "skills"),
    ("Alt+T", "cycle model thinking", "Alt+A", "cycle tool approval"),
    ("Alt+← / Alt+→", "previous / next agent", "Alt+↑ / Alt+↓", "main / last agent"),
    ("Alt+P", "plugins", "Ctrl+P", "actions"),
    ("F1", "help", "Alt+Home / Alt+End", "conversation top / bottom"),
    ("Alt+PageUp", "previous user message", "Alt+PageDown", "next user message / bottom"),
    ("Ctrl+← / Ctrl+→", "move by word", "Ctrl+A", "select all input"),
    ("Ctrl+Home / Ctrl+End", "input start / end", "Ctrl+C", "copy selection / stop task"),
    ("Ctrl+X / Ctrl+V", "cut / paste", "Ctrl+Z / Ctrl+Y", "undo / redo in composer"),
    ("Tab", "focus / switch menu domain", "Space", "toggle multi-selection"),
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
            group="COMMANDS",
        )
        for command in TUI_COMMANDS
    )
    plugins = tuple(
        Choice(
            f"plugin-command:{command.name}",
            command.usage,
            trailing=command.title,
            group="PLUGIN COMMANDS",
        )
        for command in plugin_commands
    )
    return (
        *shortcuts,
        *(
            Choice(
                f"command:{command.name}",
                command.name,
                description=(
                    "Type to filter; Space selects; "
                    "Enter opens folders or adds references; Esc closes."
                ),
                trailing=command.title,
                group="COMMANDS",
            )
            for command in REFERENCE_COMMANDS
        ),
        *commands,
        *plugins,
    )


ChoiceSelectionHandler = Callable[[str], Awaitable[tuple[Choice, ...] | None]]
ChoiceRefreshHandler = Callable[[], Awaitable[tuple[Choice, ...] | None]]
ChoiceTabHandler = Callable[[], Awaitable[None]]
ChoiceCancelHandler = Callable[[], bool]
ChoiceAdjustmentHandler = Callable[[str, int], tuple[Choice, ...] | None]
MultiChoiceConfirmationHandler = Callable[
    [tuple[str, ...]],
    Awaitable[tuple[Choice, ...] | None],
]
ChoiceEditHandler = Callable[[str, str], Awaitable[tuple[Choice, ...] | None]]
ScreenResult = TypeVar("ScreenResult")
MenuDestination = Literal["actions", "plugins", "sessions", "tasks", "skills", "help"]


@dataclass(frozen=True)
class _ChoicePage:
    navigation_id: MenuDestination | None
    retain_for_switching: bool
    title: str
    description: str
    placeholder: str
    choices: tuple[Choice, ...]
    initial_value: str | None
    filter_value: str
    on_highlight: Callable[[str], None] | None
    on_select: ChoiceSelectionHandler | None
    on_refresh: ChoiceRefreshHandler | None
    on_cancel: ChoiceCancelHandler | None
    on_adjust: ChoiceAdjustmentHandler | None
    on_tab: ChoiceTabHandler | None
    retain_focus_position_on_select: bool
    interaction_hint: str | None


class _ChoiceRowTable(Table):
    """A single table row whose cells truncate horizontally instead of wrapping."""

    content_height = 1


class _ChoiceRowVisual(RichVisual):
    def __init__(self, widget: Widget, table: _ChoiceRowTable) -> None:
        super().__init__(widget, table)
        self._height = table.content_height

    def get_height(self, rules: RulesMap, width: int) -> int:
        # Layout needs only the explicit line count, not a Rich render of every
        # off-screen table. Rich still renders visible cells at their actual width.
        return self._height


class _ChoiceGroupHeader(_ChoiceRowTable):
    """Marks a table-backed group heading without conflating it with disabled rows."""


class _ChoiceGroupHeaderText(Text):
    """Marks a text-backed group heading without conflating it with disabled rows."""


class _ChoiceOptionList(OptionList):
    """Option list with a local foreground projection for theme previews."""

    _accent_preview: Color | None = None

    def _get_visual(self, option: Option) -> Visual:
        if isinstance(option.prompt, _ChoiceRowTable):
            if option._visual is None:
                option._visual = _ChoiceRowVisual(self, option.prompt)
            return option._visual
        return super()._get_visual(option)

    def set_accent_preview(self, color: Color | None) -> None:
        if color == self._accent_preview:
            return
        self._accent_preview = color
        self.refresh()

    def get_component_rich_style(
        self,
        *names: str,
        partial: bool = False,
        default: Style | None = None,
    ) -> Style:
        style = super().get_component_rich_style(
            *names,
            partial=partial,
            default=default,
        )
        if self._accent_preview is None or "option-list--option-highlighted" not in names:
            return style
        return style + Style(color=self._accent_preview.rich_color)


def _choice_option(
    choice: Choice,
    *,
    group_accent: str,
    spinner_frame: int = 0,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> Option:
    """Render one selectable row independently from its surrounding group chrome."""

    if choice.swatches:
        palette = Text(no_wrap=True)
        leading_mark = (
            theme.spinner_frames[spinner_frame] if choice.loading else theme.session_node_mark
        )
        palette.append(leading_mark, style=choice.leading_swatch or choice.accent or "")
        palette.append("  ")
        for index, color in enumerate(choice.swatches):
            mark = (
                theme.selected_session_node_mark
                if index == choice.swatch_marker_index
                else theme.session_node_mark
            )
            palette.append(mark, style=color)
        row = cast(_ChoiceRowTable, _ChoiceRowTable.grid(expand=True, padding=0))
        row.content_height = choice.label.count("\n") + 1
        row.add_column(width=len(choice.swatches) + 3, no_wrap=True)
        row.add_column(width=2)
        row.add_column(justify="right", overflow="ellipsis", no_wrap=True)
        row.add_row(
            palette,
            "",
            Text(choice.label, style=choice.trailing_accent or choice.accent or ""),
        )
        return Option(row, id=choice.value, disabled=choice.disabled)

    label = Text(overflow="ellipsis", no_wrap=True)
    marker = theme.spinner_frames[spinner_frame] if choice.loading else choice.marker
    markers = tuple(value for value in (choice.selection_marker, marker) if value)
    if markers:
        label.append(f"{' '.join(markers)} ", style=f"bold {group_accent}")
    label.append(choice.label, style=choice.accent or "")
    if choice.trailing or choice.trailing_badge:
        row = cast(_ChoiceRowTable, _ChoiceRowTable.grid(expand=True, padding=0))
        # Let Rich shrink the wider content column first; the Text cells keep
        # both columns on one line. Only the gap expands into unused space.
        row.add_column(overflow="ellipsis")
        row.add_column(ratio=1)
        row.add_column(width=2)
        row.add_column(justify="right", overflow="ellipsis")
        trailing = Text(overflow="ellipsis", no_wrap=True)
        if choice.trailing_badge:
            trailing.append(choice.trailing_badge, style=theme.accent)
            if choice.trailing:
                trailing.append(theme.inline_separator, style=theme.separator_tone)
        trailing.append(choice.trailing, style=choice.trailing_accent or theme.text_choice)
        if choice.trailing_swatch is not None:
            trailing.append(theme.inline_separator, style=theme.separator_tone)
            trailing.append(theme.session_node_mark, style=choice.trailing_swatch)
        row.add_row(label, "", "", trailing)
        row.content_height = max(label.plain.count("\n"), trailing.plain.count("\n")) + 1
        return Option(row, id=choice.value, disabled=choice.disabled)
    if choice.description:
        label.append(
            f"\n{choice.description}",
            style=(
                theme.danger
                if choice.destructive
                else choice.description_accent or theme.text_choice
            ),
        )
    return Option(label, id=choice.value, disabled=choice.disabled)


def grouped_choice_options(
    choices: tuple[Choice, ...],
    *,
    group_accent: str | None = None,
    spinner_frame: int = 0,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> list[Option]:
    """Render choices with the shared TUI group headers and row treatment."""

    options: list[Option] = []
    resolved_group_accent = theme.text_choice if group_accent is None else group_accent
    previous_group: object = object()
    for choice in choices:
        if choice.group != previous_group:
            if choice.group:
                if isinstance(previous_group, str) and previous_group:
                    options.append(Option(Text(" "), disabled=True))
                group_header: Text | Table
                if choice.group_trailing:
                    row = cast(_ChoiceGroupHeader, _ChoiceGroupHeader.grid(expand=True, padding=0))
                    row.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
                    row.add_column(justify="right", no_wrap=True)
                    row.add_row(
                        Text(choice.group, style=f"bold {resolved_group_accent}"),
                        Text(choice.group_trailing, style=theme.text_choice),
                    )
                    row.content_height = (
                        max(choice.group.count("\n"), choice.group_trailing.count("\n")) + 1
                    )
                    group_header = row
                else:
                    group_header = _ChoiceGroupHeaderText(
                        choice.group,
                        style=f"bold {resolved_group_accent}",
                        overflow="ellipsis",
                        no_wrap=True,
                    )
                options.append(Option(group_header, disabled=True))
            previous_group = choice.group
        options.append(
            _choice_option(
                choice,
                group_accent=resolved_group_accent,
                spinner_frame=spinner_frame,
                theme=theme,
            )
        )
    return options


class ChoiceScreen(ModalScreen[str | None]):
    MIN_LIST_HEIGHT = 5
    MAX_LIST_HEIGHT = 20
    # Fixed title, filter, and hint rows surrounding the scrollable list.
    MENU_CHROME_HEIGHT = 7
    DEFAULT_HINT = DEFAULT_TUI_THEME.inline_separator.join(
        ("↑/↓ move", "←/→ group", "Enter select", "Esc close")
    )
    BACK_HINT = DEFAULT_TUI_THEME.inline_separator.join(
        ("↑/↓ move", "←/→ group", "Enter select", "Esc back")
    )

    BINDINGS = [
        Binding("tab", "switch_domain", "Switch domain", show=False, priority=True),
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
        description: str = "",
        initial_value: str | None = None,
        on_highlight: Callable[[str], None] | None = None,
        on_select: ChoiceSelectionHandler | None = None,
        on_refresh: ChoiceRefreshHandler | None = None,
        on_cancel: ChoiceCancelHandler | None = None,
        on_adjust: ChoiceAdjustmentHandler | None = None,
        on_tab: ChoiceTabHandler | None = None,
        retain_focus_position_on_select: bool = False,
        navigation_id: MenuDestination | None = None,
        retain_for_switching: bool = False,
        interaction_hint: str | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._description = description.strip()
        self._placeholder = placeholder
        self._choices = choices
        self._initial_value = initial_value
        self._on_highlight = on_highlight
        self._on_select = on_select
        self._on_refresh = on_refresh
        self._on_cancel = on_cancel
        self._on_adjust = on_adjust
        self._on_tab = on_tab
        self._retain_focus_position_on_select = retain_focus_position_on_select
        self._navigation_id = navigation_id
        self._retain_for_switching = retain_for_switching
        self._interaction_hint = interaction_hint
        self._page_stack: list[_ChoicePage] = []
        self._child_background: Color | None = None
        self._page_revision = 0
        self._spinner_frame = 0
        self._spinner_timer: Timer | None = None
        self._loading_requests: dict[object, tuple[int, str | None]] = {}
        self._loading_values: set[str | None] = set()
        self._hint: str | None = None
        self._accent_preview: Color | None = None
        self._highlight_callback_pending = False

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
            description = Static(self._description, id="choice-description", markup=False)
            description_height = self._description_height(self.app.size.height)
            description.styles.max_height = description_height
            description.display = bool(description_height)
            yield description
            yield from self._context_widgets()
            yield self._filter_input()
            choice_list = self._choice_list()
            choice_list.styles.max_height = self._responsive_list_height(self.app.size.height)
            yield choice_list
            yield Static(self._resolved_hint(), id="choice-hint")

    def _context_widgets(self) -> ComposeResult:
        """Add screen-specific context without replacing the shared menu frame."""

        return ()

    def _filter_input(self) -> Input:
        return TuiInput(
            placeholder=self._placeholder,
            id="choice-filter",
            select_on_focus=False,
        )

    def _choice_list(self) -> OptionList:
        """Build the menu body; specialized screens can replace it without copying the frame."""

        return _ChoiceOptionList(
            *self._options(
                self._choices,
                group_accent=theme_for(self).accent,
                theme=theme_for(self),
            ),
            id="choice-list",
            classes="grouped-menu-list",
        )

    def _show_hint(self, hint: str | None = None) -> None:
        """Render the current interaction contract in the shared menu footer."""

        self._hint = hint
        self._render_hint()

    def _render_hint(self) -> None:
        hint = self._resolved_hint(self._hint)
        if self._loading_values:
            visible = {
                str(option.id)
                for option in self.query_one("#choice-list", OptionList).options
                if option.id is not None and not self._choice_is_editing(str(option.id))
            }
            if not self._loading_values.intersection(visible):
                hint = f"{theme_for(self).spinner_frames[self._spinner_frame]} {hint}"
        self.query_one("#choice-hint", Static).update(hint)

    def _resolved_hint(self, hint: str | None = None) -> str:
        """Resolve page customization before the screen's navigation-aware default."""

        resolved = (
            hint
            or self._interaction_hint
            or (self.BACK_HINT if self._page_stack else self.DEFAULT_HINT)
        )
        theme = theme_for(self)
        if theme.inline_separator == DEFAULT_TUI_THEME.inline_separator:
            return resolved
        return theme.inline_separator.join(resolved.split(DEFAULT_TUI_THEME.inline_separator))

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

        self._update_description(self._description)

    def _description_height(self, viewport_height: int) -> int:
        return (
            min(3, max(0, viewport_height - self.MENU_CHROME_HEIGHT - self.MIN_LIST_HEIGHT))
            if self._description
            else 0
        )

    def _responsive_list_height(self, viewport_height: int) -> int:
        responsive_panel_height = viewport_height * 4 // 5
        description_height = self._description_height(viewport_height)
        list_height = responsive_panel_height - self.MENU_CHROME_HEIGHT - description_height
        return max(
            self.MIN_LIST_HEIGHT, min(self.MAX_LIST_HEIGHT - description_height, list_height)
        )

    def _update_description(self, description: str) -> None:
        """Keep page context separate from selectable and filterable actions."""

        self._description = description.strip()
        widget = self.query_one("#choice-description", Static)
        widget.update(self._description)
        description_height = self._description_height(self.app.size.height)
        widget.styles.max_height = description_height
        widget.display = bool(description_height)
        self.query_one("#choice-list", OptionList).styles.max_height = self._responsive_list_height(
            self.app.size.height
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
        description: str = "",
        initial_value: str | None = None,
        filter_value: str = "",
        on_highlight: Callable[[str], None] | None = None,
        on_select: ChoiceSelectionHandler | None = None,
        on_refresh: ChoiceRefreshHandler | None = None,
        on_cancel: ChoiceCancelHandler | None = None,
        on_adjust: ChoiceAdjustmentHandler | None = None,
        on_tab: ChoiceTabHandler | None = None,
        retain_focus_position_on_select: bool = False,
        navigation_id: MenuDestination | None = None,
        retain_for_switching: bool = False,
        interaction_hint: str | None = None,
    ) -> None:
        """Replace one menu level without stacking another modal screen."""

        if not self._accepts_choice_updates():
            return
        self.clear_accent_preview()
        self._page_revision += 1
        self._loading_values.clear()
        self._title = title
        self._placeholder = placeholder
        self._choices = choices
        self._on_highlight = on_highlight
        self._on_select = on_select
        self._on_refresh = on_refresh
        self._on_cancel = on_cancel
        self._on_adjust = on_adjust
        self._on_tab = on_tab
        self._retain_focus_position_on_select = retain_focus_position_on_select
        self._navigation_id = navigation_id
        self._retain_for_switching = retain_for_switching
        self._interaction_hint = interaction_hint
        self.query_one("#choice-title", Static).update(title)
        self._update_description(description)
        filter_input = self.query_one("#choice-filter", Input)
        filter_input.placeholder = placeholder
        with self.prevent(Input.Changed):
            filter_input.value = filter_value
        filter_input.cursor_position = len(filter_value)
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
        description: str = "",
        initial_value: str | None = None,
        on_highlight: Callable[[str], None] | None = None,
        on_select: ChoiceSelectionHandler | None = None,
        on_refresh: ChoiceRefreshHandler | None = None,
        on_cancel: ChoiceCancelHandler | None = None,
        on_adjust: ChoiceAdjustmentHandler | None = None,
        on_tab: ChoiceTabHandler | None = None,
        retain_focus_position_on_select: bool = False,
        retain_for_switching: bool = False,
        interaction_hint: str | None = None,
    ) -> None:
        """Switch keyboard menu destinations without creating another modal.

        A retained root remains the single Esc destination while peer roots
        replace their complete page history. Repeating the shortcut for the
        visible destination is intentionally idempotent.
        """

        if not self._accepts_choice_updates() or self._navigation_id == navigation_id:
            return
        root = self._page_stack[0] if self._page_stack else self._page()
        returning_to_root = root.retain_for_switching and navigation_id == root.navigation_id
        self._page_stack = [root] if root.retain_for_switching and not returning_to_root else []
        self.replace_page(
            navigation_id=navigation_id,
            title=title,
            description=description,
            placeholder=placeholder,
            choices=choices,
            initial_value=initial_value,
            on_highlight=on_highlight,
            on_select=on_select,
            on_refresh=on_refresh,
            on_cancel=on_cancel,
            on_adjust=on_adjust,
            on_tab=on_tab,
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
        description: str = "",
        initial_value: str | None = None,
        on_highlight: Callable[[str], None] | None = None,
        on_select: ChoiceSelectionHandler | None = None,
        on_refresh: ChoiceRefreshHandler | None = None,
        on_cancel: ChoiceCancelHandler | None = None,
        on_adjust: ChoiceAdjustmentHandler | None = None,
        on_tab: ChoiceTabHandler | None = None,
        retain_focus_position_on_select: bool = False,
        navigation_id: MenuDestination | None = None,
        retain_for_switching: bool = False,
        interaction_hint: str | None = None,
    ) -> None:
        """Navigate to a grouped submenu while retaining the current page for Esc."""

        if not self._accepts_choice_updates():
            return
        self._page_stack.append(self._page())
        self.replace_page(
            title=title,
            description=description,
            placeholder=placeholder,
            choices=choices,
            initial_value=initial_value,
            on_highlight=on_highlight,
            on_select=on_select,
            on_refresh=on_refresh,
            on_cancel=on_cancel,
            on_adjust=on_adjust,
            on_tab=on_tab,
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
            description=page.description,
            placeholder=page.placeholder,
            choices=choices if choices is not None else page.choices,
            initial_value=initial_value if initial_value is not None else page.initial_value,
            filter_value=page.filter_value if initial_value is None else "",
            on_highlight=page.on_highlight,
            on_select=page.on_select,
            on_refresh=page.on_refresh,
            on_cancel=page.on_cancel,
            on_adjust=page.on_adjust,
            on_tab=page.on_tab,
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
        revision = self._page_revision
        with self.loading_choice():
            choices = await page.on_refresh() if page.on_refresh is not None else None
        if self._accepts_choice_updates() and revision == self._page_revision:
            self.pop_page(choices=choices)
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

    def update_choices(
        self,
        choices: tuple[Choice, ...],
        *,
        expected_revision: int | None = None,
        preferred_value: str | None = None,
        title: str | None = None,
    ) -> bool:
        """Update the visible page when it still owns a background operation."""

        if not self._accepts_choice_updates() or (
            expected_revision is not None and self._page_revision != expected_revision
        ):
            return False
        previous_choices = self._choices
        self._choices = choices
        if title is not None:
            self._title = title
            self.query_one("#choice-title", Static).update(title)
        self._refresh_options(
            needle=self.query_one("#choice-filter", Input).value,
            preferred_value=preferred_value,
            previous_choices=previous_choices,
        )
        return True

    def preview_accent(self, accent: str) -> None:
        """Project one accent onto this menu without changing the App theme."""

        color = Color.parse(accent)
        if color == self._accent_preview:
            return
        self._accent_preview = color
        title = self.query_one("#choice-title", Static)
        title.styles.color = color
        title.styles.border_left = ("solid", color)
        self.query_one("#choice-description", Static).styles.border_left = ("solid", color)
        self.query_one("#choice-filter", Input).styles.border_left = ("solid", color)
        self.query_one("#choice-list", _ChoiceOptionList).set_accent_preview(color)

    def clear_accent_preview(self) -> None:
        """Return menu accent rendering to the active App theme."""

        if self._accent_preview is None or not self.is_mounted:
            self._accent_preview = None
            return
        self._accent_preview = None
        title = self.query_one("#choice-title", Static)
        title.styles.color = None
        title.styles.border_left = None
        self.query_one("#choice-description", Static).styles.border_left = None
        self.query_one("#choice-filter", Input).styles.border_left = None
        self.query_one("#choice-list", _ChoiceOptionList).set_accent_preview(None)

    async def refresh_choices(self) -> bool:
        """Refresh whichever page is still visible after background work completes."""

        if not self._accepts_choice_updates() or self._on_refresh is None:
            return False
        page_revision = self._page_revision
        choices = await self._on_refresh()
        if choices is None or not self._accepts_choice_updates():
            return False
        return self.update_choices(
            choices,
            expected_revision=page_revision,
            preferred_value=self._selected_value(),
        )

    def _accepts_choice_updates(self) -> bool:
        """Return whether this menu still owns a live render surface."""

        return self.is_mounted and self.is_attached and self.is_running and self.display

    async def prepare(self) -> None:
        """Resolve an empty menu's initial projection before mounting it."""

        if not self._choices and self._on_refresh is not None:
            self._choices = await self._on_refresh() or ()

    async def _prepare_child(self, screen: ModalScreen[ScreenResult]) -> bool:
        if not self._accepts_choice_updates() or self.app.screen is not self:
            return False
        revision = self._page_revision
        if isinstance(screen, ChoiceScreen):
            with self.loading_choice(self._selected_value()):
                await screen.prepare()
        if (
            not self._accepts_choice_updates()
            or self.app.screen is not self
            or revision != self._page_revision
        ):
            return False
        return True

    async def open_child(self, screen: ModalScreen[ScreenResult]) -> ScreenResult | None:
        """Replace this menu visually with a child and restore it on return.

        The parent remains on the screen stack so its filter, selection, and page
        history stay intact, but it must not remain visible behind the child.  A
        visible parent turns ordinary submenu navigation into nested modal chrome
        and compounds Textual's backdrop on every level.
        """

        async def wait_for_child() -> ScreenResult | None:
            if await self._prepare_child(screen):
                # Publish both visibility changes before yielding to queued callbacks.
                self._hide_for_child()
                return await asyncio.shield(self.app.push_screen(screen, wait_for_dismiss=True))
            return None

        worker: Worker[ScreenResult | None] = self.app.run_worker(
            wait_for_child,
            group=f"choice-child-{id(self)}",
            exclusive=True,
        )
        try:
            return await worker.wait()
        except WorkerCancelled as exc:
            # Shutdown or a superseding request is not a user's empty selection.
            # Callers must not resume editing a parent that no longer owns the request.
            raise asyncio.CancelledError from exc

    def show_child(self, screen: ModalScreen[ScreenResult]) -> None:
        """Show a child whose result is intentionally ignored."""

        async def push_child() -> None:
            if await self._prepare_child(screen):
                self._hide_for_child()
                await self.app.push_screen(screen)

        _worker: Worker[None] = self.app.run_worker(
            push_child,
            group=f"choice-child-{id(self)}",
            exclusive=True,
        )

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
        # Widget display changes queue a layout message. Invalidate the screen
        # now so its next repaint cannot reuse the hidden parent's compositor.
        self.refresh(layout=True)
        self.call_after_refresh(self.query_one("#choice-filter", Input).focus)

    def _page(self) -> _ChoicePage:
        return _ChoicePage(
            navigation_id=self._navigation_id,
            retain_for_switching=self._retain_for_switching,
            title=self._title,
            description=self._description,
            placeholder=self._placeholder,
            choices=self._choices,
            initial_value=self._selected_value(),
            filter_value=self.query_one("#choice-filter", Input).value,
            on_highlight=self._on_highlight,
            on_select=self._on_select,
            on_refresh=self._on_refresh,
            on_cancel=self._on_cancel,
            on_adjust=self._on_adjust,
            on_tab=self._on_tab,
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
        previous_choices: tuple[Choice, ...] | None = None,
    ) -> None:
        theme = theme_for(self)
        normalized_needle = needle.casefold().strip()
        choices = tuple(
            self._loading_choice(choice)
            for choice in self._choices
            if self._matches_filter(choice, normalized_needle)
        )
        options = self.query_one("#choice-list", OptionList)
        if previous_choices is not None:
            previous_visible = tuple(
                choice
                for choice in previous_choices
                if self._matches_filter(choice, normalized_needle)
            )
            if self._replace_stable_options(
                options,
                previous_visible,
                choices,
                theme=theme,
                preferred_value=preferred_value,
                preferred_position=preferred_position,
            ):
                self._sync_loading_timer()
                return
        options.clear_options()
        options.add_options(
            self._options(
                choices,
                group_accent=theme.accent,
                spinner_frame=self._spinner_frame,
                theme=theme,
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

    def _replace_stable_options(
        self,
        options: OptionList,
        previous: tuple[Choice, ...],
        choices: tuple[Choice, ...],
        *,
        theme: TuiTheme,
        preferred_value: str | None,
        preferred_position: int | None,
    ) -> bool:
        """Update rows with stable identities and groups without resetting navigation."""

        indexed_options = [
            (index, option) for index, option in enumerate(options.options) if option.id is not None
        ]
        if (
            len(previous) != len(choices)
            or len(indexed_options) != len(choices)
            or any(
                before.value != after.value
                or before.disabled != after.disabled
                or before.group != after.group
                or before.group_trailing != after.group_trailing
                for before, after in zip(previous, choices, strict=True)
            )
            or any(
                option.id != choice.value
                for (_, option), choice in zip(indexed_options, choices, strict=True)
            )
        ):
            return False
        for (index, _), before, after in zip(indexed_options, previous, choices, strict=True):
            if before == after:
                continue
            prompt = _choice_option(
                after,
                group_accent=theme.accent,
                spinner_frame=self._spinner_frame,
                theme=theme,
            ).prompt
            options.replace_option_prompt_at_index(index, prompt)
        highlighted = (
            self._highlight_index(
                options,
                preferred_value=preferred_value,
                preferred_position=preferred_position,
            )
            if choices
            else None
        )
        if options.highlighted != highlighted:
            options.highlighted = highlighted
        return True

    def _sync_loading_timer(self, *, force: bool = False) -> None:
        theme = theme_for(self)
        if force and self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        if (self._loading_values or any(choice.loading for choice in self._choices)) and len(
            theme.spinner_frames
        ) > 1:
            if self._spinner_timer is None:
                self._spinner_timer = self.set_interval(
                    theme.spinner_interval_seconds,
                    self._advance_loading_animation,
                )
        else:
            if self._spinner_timer is not None:
                self._spinner_timer.stop()
                self._spinner_timer = None
            self._spinner_frame = 0
        self._render_hint()

    @contextmanager
    def loading_choice(self, value: str | None = None) -> Iterator[None]:
        """Project waiting work without changing the page's authoritative choices."""

        request = object()
        self._loading_requests[request] = (self._page_revision, value)
        # Immediate callbacks finish before this message; only work that yields
        # can publish an activity frame. No artificial delay extends the operation.
        self.call_later(self._refresh_loading)
        try:
            yield
        finally:
            del self._loading_requests[request]
            self._refresh_loading()

    def _refresh_loading(self) -> None:
        if not self._accepts_choice_updates():
            return
        values = {
            value
            for revision, value in self._loading_requests.values()
            if revision == self._page_revision
        }
        previous = self._loading_values
        if values == previous:
            return
        self._loading_values = values
        self._render_loading_rows(previous | values)
        self._sync_loading_timer()

    def _choice_is_editing(self, value: str) -> bool:
        return False

    def _loading_choice(self, choice: Choice) -> Choice:
        return replace(choice, loading=True) if choice.value in self._loading_values else choice

    def _advance_loading_animation(self) -> None:
        theme = theme_for(self)
        self._spinner_frame = (self._spinner_frame + 1) % len(theme.spinner_frames)
        self._render_loading_rows(self._loading_values)
        self._render_hint()

    def _render_loading_rows(self, values: set[str | None]) -> None:
        theme = theme_for(self)
        loading = {
            choice.value: self._loading_choice(choice)
            for choice in self._choices
            if choice.loading or choice.value in values
        }
        options = self.query_one("#choice-list", OptionList)
        accent = theme.accent
        for index in range(options.option_count):
            option = options.get_option_at_index(index)
            choice = loading.get(str(option.id)) if option.id is not None else None
            if choice is None or self._choice_is_editing(choice.value):
                continue
            prompt = _choice_option(
                choice,
                group_accent=accent,
                spinner_frame=self._spinner_frame,
                theme=theme,
            ).prompt
            options.replace_option_prompt_at_index(index, prompt)

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        options = self.query_one("#choice-list", OptionList)
        if options.option_count and options.highlighted is None:
            options.highlighted = self._highlight_index(options)
        options.focus()

    def on_option_list_option_selected(
        self,
        event: OptionList.OptionSelected,
    ) -> None:
        if event.option.id is None:
            return
        event.stop()
        selected = str(event.option.id)
        if (self._page_revision, selected) in self._loading_requests.values():
            return
        # Navigation may dismiss this Screen before its destination finishes
        # loading. Keep that work application-owned and the message pump free
        # for close/prune while a selection waits for a child dialog.
        _worker: Worker[None] = self.app.run_worker(
            partial(self._run_choice, selected),
            group=f"choice-selection-{id(self)}",
            exclusive=True,
        )

    async def _run_choice(self, selected: str) -> None:
        with self.loading_choice(selected):
            try:
                await self._select_choice(selected)
            except WorkerCancelled as exc:
                raise asyncio.CancelledError from exc

    async def _select_choice(self, selected: str) -> None:
        if not self._accepts_choice_updates():
            return
        if self._on_select is None:
            self.dismiss(selected)
            return
        page_revision = self._page_revision
        choices = await self._on_select(selected)
        if (
            choices is None
            and self._page_revision == page_revision
            and self._accepts_choice_updates()
            # Navigation roots dispatch the destination asynchronously. Redrawing
            # them here would briefly render the old page before that page arrives.
            and self._navigation_id is None
            and self._on_refresh is not None
        ):
            choices = await self._on_refresh()
        if (
            choices is None
            or choices is self._choices
            or self._page_revision != page_revision
            or not self._accepts_choice_updates()
        ):
            return
        previous_choices = self._choices
        self._choices = choices
        self._refresh_options(
            needle=self.query_one("#choice-filter", Input).value,
            preferred_value=(
                None if self._retain_focus_position_on_select else self._selected_value()
            ),
            preferred_position=(
                self._highlight_position() if self._retain_focus_position_on_select else None
            ),
            previous_choices=previous_choices,
        )

    def _highlight_position(self) -> int | None:
        options = self.query_one("#choice-list", OptionList)
        highlighted = options.highlighted
        if highlighted is None:
            return None
        return sum(not options.get_option_at_index(index).disabled for index in range(highlighted))

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self._on_highlight is None or event.option.id is None:
            return
        if self._highlight_callback_pending:
            return
        self._highlight_callback_pending = True
        if not self.call_after_refresh(self._sync_highlight_callback):
            self._highlight_callback_pending = False

    def _sync_highlight_callback(self) -> None:
        """Coalesce queued highlight messages and project only the current row."""

        self._highlight_callback_pending = False
        if not self._accepts_choice_updates() or self._on_highlight is None:
            return
        selected = self._selected_value()
        if selected is not None:
            self._on_highlight(selected)

    async def action_cancel(self) -> None:
        if self._on_cancel is not None and self._on_cancel():
            return
        if self.pop_page():
            if self._on_refresh is not None:
                _worker: Worker[None] = self.run_worker(
                    self._refresh_returned_page, group="choice-return", exclusive=True
                )
            return
        self.dismiss(None)

    async def _refresh_returned_page(self) -> None:
        with self.loading_choice():
            await self.refresh_choices()

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
        if self._on_adjust is not None:
            self._adjust_choice(-1)
            return
        self._move_group(-1)

    def action_next_group(self) -> None:
        if self._on_adjust is not None:
            self._adjust_choice(1)
            return
        self._move_group(1)

    def _adjust_choice(self, direction: int) -> None:
        selected = self._selected_value()
        if selected is None or self._on_adjust is None:
            return
        choices = self._on_adjust(selected, direction)
        if choices is None or choices is self._choices:
            return
        previous_choices = self._choices
        self._choices = choices
        self._refresh_options(
            needle=self.query_one("#choice-filter", Input).value,
            preferred_value=selected,
            previous_choices=previous_choices,
        )

    def _move_group(self, direction: int) -> None:
        options = self.query_one("#choice-list", OptionList)
        starts: list[int] = []
        for index in range(options.option_count):
            if not self._is_group_header(options.get_option_at_index(index)):
                continue
            for candidate in range(index + 1, options.option_count):
                option = options.get_option_at_index(candidate)
                if self._is_group_header(option):
                    break
                if not option.disabled:
                    starts.append(candidate)
                    break
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
        if action == "switch_domain":
            return self._on_tab is not None
        if action in {"previous_group", "next_group"}:
            return self._on_adjust is not None or any(choice.group for choice in self._choices)
        return super().check_action(action, parameters)

    def action_accept_choice(self) -> None:
        self.query_one("#choice-list", OptionList).action_select()

    async def action_switch_domain(self) -> None:
        if self._on_tab is not None:
            _worker: Worker[None] = self.run_worker(
                self._switch_domain, group="choice-domain", exclusive=True
            )

    async def _switch_domain(self) -> None:
        with self.loading_choice():
            if self._on_tab is not None:
                await self._on_tab()

    @staticmethod
    def _options(
        choices: tuple[Choice, ...],
        *,
        group_accent: str | None = None,
        spinner_frame: int = 0,
        theme: TuiTheme = DEFAULT_TUI_THEME,
    ) -> list[Option]:
        return grouped_choice_options(
            choices,
            group_accent=group_accent,
            spinner_frame=spinner_frame,
            theme=theme,
        )

    def refresh_theme(self) -> None:
        self._spinner_frame = 0
        self._sync_loading_timer(force=True)
        self._refresh_options(
            needle=self.query_one("#choice-filter", Input).value,
            preferred_value=self._selected_value(),
        )
        self._show_hint()

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
            or normalized_needle in choice.value.casefold()
            or normalized_needle in choice.description.casefold()
            or normalized_needle in choice.group.casefold()
            or normalized_needle in choice.trailing.casefold()
            or normalized_needle in choice.trailing_badge.casefold()
        )


class _MultiChoiceFilterInput(TuiInput):
    BINDINGS = [
        Binding("space", "toggle_choice", "Toggle", show=False, priority=True),
    ]

    def action_toggle_choice(self) -> None:
        screen = self.screen
        if isinstance(screen, MultiChoiceScreen):
            screen.action_toggle_choice()


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
                    style=f"not bold {theme.text_choice} italic",
                )
            prompt.truncate(self._editor_width(), overflow="ellipsis")
            options.replace_option_prompt_at_index(option_index, self._editor_prompt(prompt))
            return
        editor_width = self._editor_width()
        view_start, view_end = self._editor_view_bounds(display_text, editor_width)
        if view_start:
            prompt.append("…", style=f"not bold {theme.text_choice}")
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
            prompt.append("…", style=f"not bold {theme.text_choice}")
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


class MultiChoiceScreen(ChoiceScreen):
    """Keep a filtered selection set while Enter confirms one or many choices."""

    DEFAULT_HINT = DEFAULT_TUI_THEME.inline_separator.join(
        ("↑/↓ move", "←/→ group", "Space toggle", "Enter confirm", "Esc close")
    )
    BACK_HINT = DEFAULT_TUI_THEME.inline_separator.join(
        ("↑/↓ move", "←/→ group", "Space toggle", "Enter confirm", "Esc back")
    )
    CONFIRM_HINT = DEFAULT_TUI_THEME.inline_separator.join(
        ("↑/↓ move", "Enter select", "Esc cancel")
    )

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
        destructive: bool = False,
        selected_values: tuple[str, ...] = (),
        interaction_hint: str | None = None,
    ) -> None:
        super().__init__(
            title=title, placeholder=placeholder, choices=choices, interaction_hint=interaction_hint
        )
        self._on_confirm = on_confirm
        self._selected = set(selected_values).intersection(choice.value for choice in choices)
        self._selected_marker = selected_marker
        self._unselected_marker = unselected_marker
        self._confirm_label = confirm_label
        self._destructive = destructive
        self._confirmation_values: tuple[str, ...] = ()
        self._catalog_choices: tuple[Choice, ...] = ()
        self._catalog_filter = ""
        self._choices = self._project_markers(self._choices)

    def _filter_input(self) -> Input:
        return _MultiChoiceFilterInput(
            placeholder=self._placeholder,
            id="choice-filter",
            select_on_focus=False,
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
            theme=theme_for(self),
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
            values = self._confirmation_values
            revision = self._page_revision
            choices = await self._on_confirm(values)
            if not self._accepts_choice_updates() or revision != self._page_revision:
                return
            if self._confirmation_values is values:
                self._leave_confirmation(self._catalog_choices if choices is None else choices)
            elif not self._confirmation_values and choices is not None:
                self._selected.intersection_update(choice.value for choice in choices)
                self.update_choices(
                    self._project_markers(choices), preferred_value=self._selected_value()
                )
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
                f"{self._confirm_label} selected ({len(selected)})",
                description=(
                    "Permanently delete the selected items? This cannot be undone."
                    if self._destructive
                    else ""
                ),
                destructive=self._destructive,
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

    DEFAULT_HINT = DEFAULT_TUI_THEME.inline_separator.join(
        ("↑/↓ move", "Enter select", "Esc cancel")
    )
    BACK_HINT = DEFAULT_HINT
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
                Choice(self.CONFIRM_VALUE, confirm_label, description=message, destructive=True),
            ),
            initial_value="__confirmation_cancel__",
        )

    def on_mount(self) -> None:
        super().on_mount()
        self.call_after_refresh(self.query_one("#choice-list", OptionList).focus)

    @classmethod
    def confirmed(cls, selected: str | None) -> bool:
        return selected == cls.CONFIRM_VALUE
