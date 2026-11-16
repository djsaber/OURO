"""Configuration screens for tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from ouro.application.contracts.configuration import ToolExecutionConfigView
from ouro.core.tool_batches import ToolExecutionMode
from ouro.core.tools import PermissionMode
from ouro.tui.features.configuration.values import _positive_int
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.editable import EditableChoiceScreen
from ouro.tui.menus.labels import PERMISSION_LABELS, TOOL_APPROVAL_LABEL
from ouro.tui.menus.models import Choice
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, theme_for
from ouro.tui.view_models import TuiToolConfiguration, TuiToolExecutionConfiguration

_TOOL_PERMISSION_CHOICES: dict[str, PermissionMode | None] = {
    "default": None,
    "auto": "auto",
    "approval": "approval",
    "deny": "deny",
}

_TOOL_SOURCE_LABELS = {
    "builtin": "Built-in",
    "mcp": "MCP",
    "official_plugin": "Plugin",
    "third_party_plugin": "Plugin",
    "ai_generated_plugin": "Plugin",
    "unavailable": "Unavailable",
}
_TOOL_SOURCE_ORDER = {"Unavailable": 0, "Built-in": 1, "MCP": 2, "Plugin": 3}


class ToolConfigurationScreen(EditableChoiceScreen):
    """Manage tool execution and permissions in one grouped menu."""

    _MODES = ("sequential", "parallel")

    def __init__(
        self,
        config: ToolExecutionConfigView,
        *,
        tools: tuple[TuiToolConfiguration, ...],
        on_permission: Callable[
            [str, PermissionMode | None], Awaitable[tuple[TuiToolConfiguration, ...] | None]
        ],
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._on_permission = on_permission
        self._values = {
            "tool-execution-mode": config.mode,
            "tool-max-concurrency": str(config.max_concurrency),
            "tool-max-output": str(config.max_output_chars),
        }
        super().__init__(
            title="TOOL",
            placeholder="Filter tools…",
            choices=self._tool_choices(),
            on_save=self._save_field,
        )

    def _matches_filter(self, choice: Choice, normalized_needle: str) -> bool:
        if choice.value.startswith("tool-source:"):
            source = choice.value.removeprefix("tool-source:")
            return any(
                self._matches_filter(row, normalized_needle)
                for row in self._choices
                if row.value.startswith("permission:")
                and _TOOL_SOURCE_LABELS[self._tools[row.value.removeprefix("permission:")].source]
                == source
            )
        if choice.value.startswith("permission:"):
            tool = self._tools.get(choice.value.removeprefix("permission:"))
            if (
                tool is not None
                and normalized_needle in _TOOL_SOURCE_LABELS[tool.source].casefold()
            ):
                return True
        return super()._matches_filter(choice, normalized_needle)

    async def _select_choice(self, selected: str) -> None:
        if self._editing_value is None and selected.startswith("permission:"):
            name = selected.removeprefix("permission:")
            tool = self._tools[name]
            if tool.permission_control == "tool":
                return
            current_permission = tool.permission_override or "default"
            theme = theme_for(self)

            async def save_permission(value: str) -> tuple[Choice, ...] | None:
                tools = await self._on_permission(name, _TOOL_PERMISSION_CHOICES[value])
                if tools is not None:
                    self._tools = {tool.name: tool for tool in tools}
                    self.update_choices(self._tool_choices(), preferred_value=selected)
                    if permissions.is_mounted and self.app.screen is permissions:
                        permissions.dismiss(None)
                return None

            permissions = ChoiceScreen(
                title=f"{TOOL_APPROVAL_LABEL.upper()}{theme.inline_separator}{tool.display_name}",
                placeholder=None,
                choices=_tool_permission_choices(
                    current_permission, default_permission=tool.inherited_permission, theme=theme
                ),
                initial_value=current_permission,
                on_select=save_permission,
            )
            await self.open_child(permissions)
            return
        if self._editing_value is None and selected == "tool-execution-mode":
            current = self._values[selected]
            self._values[selected] = self._MODES[(self._MODES.index(current) + 1) % 2]
            self._refresh(selected)
            return
        if self._editing_value is None and selected == "tool-config-save":
            self._submit()
            return
        await super()._select_choice(selected)

    async def _save_field(self, field_id: str, value: str) -> tuple[Choice, ...] | None:
        self._values[field_id] = value.strip()
        return self._tool_choices()

    def _refresh(self, selected: str) -> None:
        self._replace_choices(self._tool_choices(), selected=selected)

    def _draft(self) -> TuiToolExecutionConfiguration:
        return TuiToolExecutionConfiguration(
            mode=cast(ToolExecutionMode, self._values["tool-execution-mode"]),
            max_concurrency=_positive_int(
                self._values["tool-max-concurrency"], "Parallel tool limit"
            ),
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

    def _tool_choices(self) -> tuple[Choice, ...]:
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
            *_tool_choices(tuple(self._tools.values())),
            Choice(
                "tool-config-save",
                "Save",
                trailing=error,
                group="ACTIONS",
                disabled=bool(error),
            ),
        )


def _tool_choices(tools: tuple[TuiToolConfiguration, ...]) -> tuple[Choice, ...]:
    ordered = sorted(
        tools,
        key=lambda tool: (
            _TOOL_SOURCE_ORDER[_TOOL_SOURCE_LABELS[tool.source]],
            (tool.plugin_name or tool.plugin_id or "").casefold(),
            (tool.plugin_id or "").casefold(),
            tool.display_name.casefold(),
            tool.name.casefold(),
        ),
    )
    choices: list[Choice] = []
    previous_source = None
    for tool in ordered:
        source = _TOOL_SOURCE_LABELS[tool.source]
        if source != previous_source and source != "Unavailable":
            choices.append(
                Choice(f"tool-source:{source}", f"\n{source}", group="PERMISSIONS", disabled=True)
            )
        previous_source = source
        plugin_name = tool.plugin_name or tool.plugin_id
        label = tool.display_name
        if source == "Plugin" and plugin_name:
            label = f"{plugin_name}: {label}"
        elif source == "Unavailable":
            label = f"{source}: {label}"
        choices.append(
            Choice(
                f"permission:{tool.name}",
                label,
                group="PERMISSIONS",
                trailing=PERMISSION_LABELS[
                    (tool.effective_permission if tool.available else None) or "unavailable"
                ],
                disabled=tool.permission_control == "tool",
            )
        )
    return tuple(choices)


def _tool_permission_choices(
    current: str,
    *,
    default_permission: PermissionMode | None,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[Choice, ...]:
    return tuple(
        Choice(
            value,
            PERMISSION_LABELS[value],
            trailing=(
                PERMISSION_LABELS[default_permission]
                if value == "default" and default_permission is not None
                else ""
            ),
            marker=theme.selected_choice_marker if value == current else "",
        )
        for value in _TOOL_PERMISSION_CHOICES
    )
