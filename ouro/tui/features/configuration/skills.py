"""Configuration screens for skills."""

from __future__ import annotations

from typing import cast

from ouro.application.contracts.configuration import SkillInstallScopeInput
from ouro.tui.features.configuration.values import _enabled_label
from ouro.tui.menus.editable import EditableChoiceScreen
from ouro.tui.menus.models import Choice
from ouro.tui.view_models import TuiSkillInstallConfiguration


class SkillInstallScreen(EditableChoiceScreen):
    """Collect a skill installation through the shared grouped menu."""

    def __init__(self) -> None:
        self._values = {
            "skill-source": "",
            "skill-scope": "user",
            "skill-selector": "",
            "skill-ref": "",
        }
        self._toggles = {"skill-install-all": False, "skill-force": False}
        super().__init__(
            title="INSTALL SKILL",
            placeholder="Filter skill settings…",
            choices=self._skill_choices(),
            on_save=self._save_field,
        )

    async def _select_choice(self, selected: str) -> None:
        if self._editing_value is None and selected == "skill-scope":
            self._values[selected] = "workspace" if self._values[selected] == "user" else "user"
            self._refresh(selected)
            return
        if self._editing_value is None and selected in self._toggles:
            self._toggles[selected] = not self._toggles[selected]
            self._refresh(selected)
            return
        if selected == "skill-install" and self._editing_value is None:
            self._submit()
            return
        await super()._select_choice(selected)

    async def _save_field(self, field_id: str, value: str) -> tuple[Choice, ...] | None:
        self._values[field_id] = value.strip()
        return self._skill_choices()

    def _refresh(self, selected: str) -> None:
        self._replace_choices(self._skill_choices(), selected=selected)

    def _submit(self) -> None:
        try:
            draft = self._draft()
        except ValueError:
            return
        self.dismiss(draft)  # type: ignore[arg-type]

    def _draft(self) -> TuiSkillInstallConfiguration:
        source = self._values["skill-source"]
        selector = self._values["skill-selector"] or None
        install_all = self._toggles["skill-install-all"]
        if not source:
            raise ValueError("Source is required")
        if selector is not None and install_all:
            raise ValueError("Choose skill names or Install all, not both")
        return TuiSkillInstallConfiguration(
            source=source,
            scope=cast(SkillInstallScopeInput, self._values["skill-scope"]),
            selector=selector,
            install_all=install_all,
            force=self._toggles["skill-force"],
            ref=self._values["skill-ref"] or None,
        )

    def _validation_error(self) -> str:
        try:
            self._draft()
        except ValueError as exc:
            return str(exc)
        return ""

    def _skill_choices(self) -> tuple[Choice, ...]:
        error = self._validation_error()
        return (
            Choice(
                "skill-source",
                "Source",
                trailing=self._values["skill-source"] or "Required",
                group="SOURCE",
                edit_value=self._values["skill-source"],
            ),
            Choice(
                "skill-scope", "Scope", trailing=self._values["skill-scope"], group="DESTINATION"
            ),
            Choice(
                "skill-selector",
                "Skill names",
                trailing=self._values["skill-selector"] or "All available skills",
                group="SELECTION",
                edit_value=self._values["skill-selector"],
            ),
            Choice(
                "skill-install-all",
                "Install all",
                trailing=_enabled_label(self._toggles["skill-install-all"]),
                group="SELECTION",
            ),
            Choice(
                "skill-ref",
                "Branch, tag or commit",
                trailing=self._values["skill-ref"] or "Default",
                group="VERSION",
                edit_value=self._values["skill-ref"],
            ),
            Choice(
                "skill-force",
                "Replace existing",
                trailing=_enabled_label(self._toggles["skill-force"]),
                group="OPTIONS",
            ),
            Choice(
                "skill-install", "Install", trailing=error, group="ACTIONS", disabled=bool(error)
            ),
        )
