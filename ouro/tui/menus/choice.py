"""Single-choice menu navigation and selection."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import partial
from typing import cast

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerCancelled

from ouro.tui.menus.models import (
    Choice,
    ChoiceAdjustmentHandler,
    ChoiceCancelHandler,
    ChoiceRefreshHandler,
    ChoiceSelectionHandler,
    ChoiceTabHandler,
    MenuDestination,
    ScreenResult,
)
from ouro.tui.menus.rows import (
    _choice_option,
    _ChoiceGroupHeader,
    _ChoiceGroupHeaderText,
    _ChoiceOptionList,
    grouped_choice_options,
)
from ouro.tui.text_inputs import TuiInput
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, label_text, theme_for


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
            yield Static(label_text(self._title, theme=theme_for(self)), id="choice-title")
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
        self.app.screen_change_signal.subscribe(
            self, self._on_current_screen_changed, immediate=True
        )
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
        self.query_one("#choice-title", Static).update(label_text(title, theme=theme_for(self)))
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
            self.query_one("#choice-title", Static).update(label_text(title, theme=theme_for(self)))
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
        self.query_one("#choice-list", _ChoiceOptionList).set_accent_preview(color)

    def clear_accent_preview(self) -> None:
        """Return menu accent rendering to the active App theme."""

        if self._accent_preview is None or not self.is_mounted:
            self._accent_preview = None
            return
        self._accent_preview = None
        title = self.query_one("#choice-title", Static)
        title.styles.color = None
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

        self._restore_after_child()

    def _on_current_screen_changed(self, screen: Screen) -> None:
        if screen is self:
            # The stack changes synchronously, but ScreenResume is queued. A
            # pending repaint can run first, so restore visibility at the stack
            # boundary rather than allowing that repaint to draw a hidden menu.
            self._restore_after_child()

    def _restore_after_child(self) -> None:
        background = self._child_background
        if background is None:
            return
        self._child_background = None
        self.styles.background = background
        self.query_one("#choice-panel", Vertical).display = True
        # Widget display changes queue a layout message. Invalidate the screen
        # now so its next repaint cannot reuse the hidden parent's compositor.
        self.refresh(layout=True)
        # Returning to the parent owns this focus change; the child result may select another
        # control before the next frame, so do not defer it past that result.
        self.query_one("#choice-filter", Input).focus()

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
        title = self.query_one("#choice-title", Static)
        title.update(label_text(cast(Text, title.content).plain, theme=theme_for(self)))
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
