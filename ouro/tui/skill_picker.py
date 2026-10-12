"""Skill selection projected onto the shared composer reference menu."""

from __future__ import annotations

from dataclasses import replace

from ouro.tui.commands import TuiCommandArgument
from ouro.tui.theme import TuiTheme


class SkillPicker:
    def __init__(self, options: tuple[TuiCommandArgument, ...]) -> None:
        self.options = options
        self.selected: list[str] = []

    def toggle(self, value: str) -> None:
        if value in self.selected:
            self.selected.remove(value)
        elif any(item.value == value for item in self.options):
            self.selected.append(value)

    def skills_for(self, value: str) -> tuple[str, ...]:
        if self.selected:
            return tuple(self.selected)
        return tuple(item.value for item in self.options if item.value == value)

    def arguments(self, query: str, theme: TuiTheme) -> tuple[TuiCommandArgument, ...]:
        needle = query.casefold().strip()
        return tuple(
            replace(
                item,
                checked=item.value in self.selected,
                accent=theme.accent if item.value in self.selected else None,
            )
            for item in self.options
            if needle in item.value.casefold()
        )
