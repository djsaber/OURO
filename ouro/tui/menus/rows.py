"""Menu option row widgets and selection indicators."""

from __future__ import annotations

from typing import cast

from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual.color import Color
from textual.css.styles import RulesMap
from textual.visual import RichVisual, Visual
from textual.widget import Widget
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from ouro.tui.menus.models import Choice
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, label_text


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
            label_text(
                choice.label, theme=theme, style=choice.trailing_accent or choice.accent or ""
            ),
        )
        return Option(row, id=choice.value, disabled=choice.disabled)

    label = Text(overflow="ellipsis", no_wrap=True)
    marker = theme.spinner_frames[spinner_frame] if choice.loading else choice.marker
    markers = tuple(value for value in (choice.selection_marker, marker) if value)
    if markers:
        label.append(f"{' '.join(markers)} ", style=f"bold {group_accent}")
    label.append(label_text(choice.label, theme=theme, style=choice.accent or ""))
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
                trailing.append(theme.inline_separator, style=theme.text_lowlight)
        trailing.append(
            label_text(
                choice.trailing, theme=theme, style=choice.trailing_accent or theme.text_lowlight
            )
        )
        if choice.trailing_swatch is not None:
            trailing.append(theme.inline_separator, style=theme.text_lowlight)
            trailing.append(theme.session_node_mark, style=choice.trailing_swatch)
        row.add_row(label, "", "", trailing)
        row.content_height = max(label.plain.count("\n"), trailing.plain.count("\n")) + 1
        return Option(row, id=choice.value, disabled=choice.disabled)
    if choice.description:
        label.append(
            label_text(
                f"\n{choice.description}",
                theme=theme,
                style=(
                    theme.danger
                    if choice.destructive
                    else choice.description_accent or theme.text_lowlight
                ),
            )
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
    resolved_group_accent = theme.text_emphasis if group_accent is None else group_accent
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
                        label_text(
                            choice.group, theme=theme, style=f"bold {resolved_group_accent}"
                        ),
                        label_text(choice.group_trailing, theme=theme, style=theme.text_lowlight),
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
