"""Plugin source selection and installation screen."""

from __future__ import annotations

from ouro.tui.menus.editable import EditableChoiceScreen
from ouro.tui.menus.models import Choice
from ouro.tui.view_models import TuiPluginInstallConfiguration


class PluginInstallScreen(EditableChoiceScreen):
    """Collect an install source using the same inline form as other resources."""

    def __init__(self) -> None:
        self._values = {"source": "", "ref": "", "subdirectory": ""}
        super().__init__(
            title="INSTALL PLUGIN",
            placeholder="Filter plugin install settings…",
            choices=self._plugin_install_choices(),
            on_save=self._save_field,
        )

    async def _select_choice(self, selected: str) -> None:
        if self._editing_value is None and selected == "plugin-install":
            self._submit()
            return
        await super()._select_choice(selected)

    async def _save_field(self, field_id: str, value: str) -> tuple[Choice, ...] | None:
        self._values[field_id.removeprefix("plugin-")] = value.strip()
        return self._plugin_install_choices()

    def _submit(self) -> None:
        source = self._values["source"]
        if not source:
            return
        draft = TuiPluginInstallConfiguration(
            source=source,
            ref=self._values["ref"] or None,
            subdirectory=self._values["subdirectory"] or None,
        )
        self.dismiss(draft)  # type: ignore[arg-type]

    def _plugin_install_choices(self) -> tuple[Choice, ...]:
        source = self._values["source"]
        return (
            Choice(
                "plugin-source",
                "Source",
                trailing=source or "Required",
                group="SOURCE",
                edit_value=source,
                edit_placeholder="Enter a local path, Git URL, or archive URL",
            ),
            Choice(
                "plugin-ref",
                "Branch, tag or commit",
                trailing=self._values["ref"] or "Default",
                group="SOURCE",
                edit_value=self._values["ref"],
                edit_placeholder="Optional branch, tag, or commit",
            ),
            Choice(
                "plugin-subdirectory",
                "Subdirectory",
                trailing=self._values["subdirectory"] or "Root",
                group="SOURCE",
                edit_value=self._values["subdirectory"],
                edit_placeholder="Optional package path inside the repository",
            ),
            Choice(
                "plugin-install",
                "Install",
                trailing="" if source else "Source is required",
                group="ACTIONS",
                disabled=not source,
            ),
        )
