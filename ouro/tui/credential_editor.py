from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ouro.tui.screens import Choice, ChoiceEditHandler, ChoiceTabHandler, EditableChoiceScreen
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, theme_for


@dataclass(frozen=True)
class CredentialEdit:
    source: Literal["inline", "env"]
    value: str = field(repr=False)


def credential_trailing(edit: CredentialEdit, *, configured: bool, theme: TuiTheme) -> str:
    if not configured:
        return ""
    mask = theme.mask_character * 5
    return f"{mask} [{'KEY' if edit.source == 'inline' else 'ENV'}]"


class CredentialEditableChoiceScreen(EditableChoiceScreen):
    """Edit credentials in place, retaining a separate input buffer for each source."""

    CREDENTIAL_EDIT_HINT = DEFAULT_TUI_THEME.inline_separator.join(
        ("←/→ cursor", "Tab type", "Enter save", "Esc cancel")
    )

    def __init__(
        self,
        *,
        title: str,
        placeholder: str,
        choices: tuple[Choice, ...],
        on_save: ChoiceEditHandler,
        on_tab: ChoiceTabHandler | None = None,
    ) -> None:
        self._credential_source: Literal["inline", "env"] | None = None
        self._credential_buffers: dict[str, str] = {}
        super().__init__(
            title=title, placeholder=placeholder, choices=choices, on_save=on_save, on_tab=on_tab
        )

    def _credential_value(self, field_id: str) -> CredentialEdit | None:
        raise NotImplementedError

    async def _save_credential(
        self, field_id: str, edit: CredentialEdit
    ) -> tuple[Choice, ...] | None:
        raise NotImplementedError

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "switch_domain" and self._credential_source is not None:
            return True
        return super().check_action(action, parameters)

    def _begin_edit(self, selected: str) -> None:
        choice = next((choice for choice in self._choices if choice.value == selected), None)
        if choice is None or choice.disabled:
            return
        edit = self._credential_value(selected)
        self._credential_source = edit.source if edit is not None else None
        self._credential_buffers = {"inline": "", "env": ""}
        super()._begin_edit(selected)
        if edit is not None:
            self._show_hint(self.CREDENTIAL_EDIT_HINT)

    async def action_switch_domain(self) -> None:
        if self._credential_source is None:
            await super().action_switch_domain()
            return
        self._credential_buffers[self._credential_source] = self._edit_text
        self._credential_source = "env" if self._credential_source == "inline" else "inline"
        self._edit_text = self._credential_buffers[self._credential_source]
        self._edit_cursor = len(self._edit_text)
        self._edit_view_start = 0
        self._reset_edit_cursor()

    async def _select_choice(self, selected: str) -> None:
        if self._editing_value == selected and self._credential_source is not None:
            await self._save_edit(
                self._save_credential(
                    selected, CredentialEdit(self._credential_source, self._edit_text)
                ),
                selected=selected,
            )
            return
        await super()._select_choice(selected)

    def _edit_snapshot(self) -> tuple[str | None, ...]:
        return (*super()._edit_snapshot(), self._credential_source)

    def _finish_edit(self, choices: tuple[Choice, ...], *, selected: str) -> None:
        self._credential_source = None
        self._credential_buffers.clear()
        super()._finish_edit(choices, selected=selected)

    def _editor_trailing(self) -> str:
        if self._credential_source is None:
            return ""
        return "[KEY]" if self._credential_source == "inline" else "[ENV]"

    def _editor_placeholder(self) -> str:
        placeholder = super()._editor_placeholder()
        if self._credential_source is None:
            return placeholder
        prompt = "Enter key" if self._credential_source == "inline" else "env:VARIABLE"
        return f"{prompt} — {placeholder}"

    def _editor_display_text(self, value: str) -> str:
        return theme_for(self).mask_character * len(value) if self._credential_source else value
