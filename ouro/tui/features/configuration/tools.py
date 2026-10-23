"""Configuration screens for tools."""

from __future__ import annotations

from typing import cast

from ouro.application.contracts.configuration import ToolExecutionConfigView
from ouro.core.tool_batches import ToolExecutionMode
from ouro.tui.features.configuration.values import _enabled_label, _positive_int
from ouro.tui.menus.editable import EditableChoiceScreen
from ouro.tui.menus.models import Choice
from ouro.tui.view_models import TuiToolExecutionConfiguration


class ToolExecutionConfigurationScreen(EditableChoiceScreen):
    """Edit tool execution and path access through the shared grouped menu."""

    _MODES = ("sequential", "parallel")

    def __init__(self, config: ToolExecutionConfigView) -> None:
        self._values = {
            "tool-execution-mode": config.mode,
            "tool-max-concurrency": str(config.max_concurrency),
            "tool-max-output": str(config.max_output_chars),
        }
        self._workspace_only = config.workspace_only
        super().__init__(
            title="TOOL LIMITS & FILE ACCESS",
            placeholder="Search tool settings…",
            choices=self._tool_execution_choices(),
            on_save=self._save_field,
        )

    async def _select_choice(self, selected: str) -> None:
        if self._editing_value is None and selected == "tool-execution-mode":
            current = self._values[selected]
            self._values[selected] = self._MODES[(self._MODES.index(current) + 1) % 2]
            self._refresh(selected)
            return
        if self._editing_value is None and selected == "tool-workspace-only":
            self._workspace_only = not self._workspace_only
            self._refresh(selected)
            return
        if self._editing_value is None and selected == "tool-config-save":
            self._submit()
            return
        await super()._select_choice(selected)

    async def _save_field(self, field_id: str, value: str) -> tuple[Choice, ...] | None:
        self._values[field_id] = value.strip()
        return self._tool_execution_choices()

    def _refresh(self, selected: str) -> None:
        self._replace_choices(self._tool_execution_choices(), selected=selected)

    def _draft(self) -> TuiToolExecutionConfiguration:
        return TuiToolExecutionConfiguration(
            mode=cast(ToolExecutionMode, self._values["tool-execution-mode"]),
            max_concurrency=_positive_int(
                self._values["tool-max-concurrency"], "Parallel tool limit"
            ),
            workspace_only=self._workspace_only,
            max_output_chars=_positive_int(
                self._values["tool-max-output"], "Max output characters"
            ),
        )

    def _validation_error(self) -> str:
        try:
            self._draft()
        except ValueError as exc:
            return str(exc)
        return ""

    def _submit(self) -> None:
        try:
            draft = self._draft()
        except ValueError:
            return
        self.dismiss(draft)  # type: ignore[arg-type]

    def _tool_execution_choices(self) -> tuple[Choice, ...]:
        error = self._validation_error()
        return (
            Choice(
                "tool-execution-mode",
                "Run tools",
                trailing=(
                    "One at a time"
                    if self._values["tool-execution-mode"] == "sequential"
                    else "In parallel"
                ),
                group="EXECUTION",
            ),
            Choice(
                "tool-max-concurrency",
                "Parallel tool limit",
                trailing=self._values["tool-max-concurrency"] or "Required",
                group="EXECUTION",
                edit_value=self._values["tool-max-concurrency"],
                edit_placeholder="Enter a positive integer",
            ),
            Choice(
                "tool-max-output",
                "Max output characters",
                trailing=self._values["tool-max-output"] or "Required",
                group="EXECUTION",
                edit_value=self._values["tool-max-output"],
                edit_placeholder="Enter a positive integer",
            ),
            Choice(
                "tool-workspace-only",
                "Restrict paths to workspace",
                trailing=_enabled_label(self._workspace_only),
                group="PATH ACCESS",
            ),
            Choice(
                "tool-config-save",
                "Save",
                trailing=error,
                group="ACTIONS",
                disabled=bool(error),
            ),
        )
