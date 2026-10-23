"""Configuration screens for mcp."""

from __future__ import annotations

from typing import cast

from ouro.application.contracts.configuration import MCPServerConfigView, MCPTransportInput
from ouro.core.tools import PermissionMode, ToolEffect
from ouro.tui.features.configuration.values import (
    _enabled_label,
    _non_negative_int,
    _optional_json_mapping,
    _optional_json_string_list,
    _positive_float,
)
from ouro.tui.menus.editable import EditableChoiceScreen
from ouro.tui.menus.labels import PERMISSION_LABELS
from ouro.tui.menus.models import Choice
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, theme_for
from ouro.tui.view_models import TuiMCPServerConfiguration


class MCPConfigurationScreen(EditableChoiceScreen):
    """Edit MCP settings through the shared grouped inline-edit menu."""

    _TRANSPORTS = ("stdio", "http", "sse")
    _PERMISSIONS = ("auto", "approval", "deny")
    _TOGGLES = frozenset({"mcp-enabled", "mcp-auto-start", "mcp-auto-reconnect"})
    _MASKED = frozenset({"mcp-env", "mcp-headers", "mcp-credential-refs"})

    def __init__(
        self,
        existing: MCPServerConfigView | None = None,
        *,
        theme: TuiTheme = DEFAULT_TUI_THEME,
    ) -> None:
        self._initial_tui_theme = theme
        self._existing = existing
        transport = existing.transport if existing is not None else "stdio"
        self._values = {
            "mcp-name": existing.name if existing is not None else "",
            "mcp-transport": transport,
            "mcp-target": (
                (existing.command if transport == "stdio" else existing.url) or ""
                if existing is not None
                else ""
            ),
            "mcp-args": "",
            "mcp-cwd": existing.cwd or "" if existing is not None else "",
            "mcp-env": "",
            "mcp-inherit-env": "",
            "mcp-headers": "",
            "mcp-credential-refs": "",
            "mcp-permission": existing.permission_mode if existing is not None else "approval",
            "mcp-tool-effects": "",
            "mcp-reconnect-attempts": str(
                existing.max_reconnect_attempts if existing is not None else 3
            ),
            "mcp-reconnect-backoff": str(
                existing.reconnect_backoff_seconds if existing is not None else 0.5
            ),
            "mcp-timeout": str(existing.timeout_seconds if existing is not None else 30.0),
            "mcp-health-interval": str(
                existing.health_check_interval_seconds if existing is not None else 30.0
            ),
        }
        self._toggles = {
            "mcp-enabled": existing.enabled if existing is not None else True,
            "mcp-auto-start": existing.auto_start if existing is not None else False,
            "mcp-auto-reconnect": (existing.auto_reconnect if existing is not None else True),
        }
        super().__init__(
            title="EDIT MCP" if existing is not None else "ADD MCP",
            placeholder="Filter MCP settings…",
            choices=self._mcp_choices(),
            on_save=self._save_field,
        )

    async def _select_choice(self, selected: str) -> None:
        if self._editing_value is None and selected in self._TOGGLES:
            self._toggles[selected] = not self._toggles[selected]
            self._refresh(selected)
            return
        cycles = {
            "mcp-transport": self._TRANSPORTS,
            "mcp-permission": self._PERMISSIONS,
        }
        if self._editing_value is None and selected in cycles:
            values = cycles[selected]
            current = self._values[selected]
            self._values[selected] = values[(values.index(current) + 1) % len(values)]
            self._refresh(selected)
            return
        if selected == "mcp-save" and self._editing_value is None:
            self._submit()
            return
        await super()._select_choice(selected)

    async def _save_field(self, field_id: str, value: str) -> tuple[Choice, ...] | None:
        self._values[field_id] = value.strip()
        return self._mcp_choices()

    def _editor_display_text(self, value: str) -> str:
        return (
            theme_for(self).mask_character * len(value)
            if self._editing_value in self._MASKED
            else value
        )

    def _refresh(self, selected: str) -> None:
        self._replace_choices(self._mcp_choices(), selected=selected)

    def _submit(self) -> None:
        try:
            draft = self._draft()
        except ValueError:
            return
        self.dismiss(draft)  # type: ignore[arg-type]

    def _draft(self) -> TuiMCPServerConfiguration:
        name = self._values["mcp-name"]
        if not name:
            raise ValueError("Display name is required")
        transport = cast(MCPTransportInput, self._values["mcp-transport"])
        target = self._values["mcp-target"] or None
        if (self._existing is None or transport != self._existing.transport) and not target:
            raise ValueError("Command or URL is required for a new transport")
        effects = _optional_json_mapping(self._values["mcp-tool-effects"], "Tool effect overrides")
        if effects is not None and any(
            value not in {"read_only", "state_changing", "unknown"} for value in effects.values()
        ):
            raise ValueError("Tool effects must be read_only, state_changing, or unknown")
        return TuiMCPServerConfiguration(
            server_id=self._existing.server_id if self._existing is not None else None,
            name=name,
            transport=transport,
            target=target,
            args=_optional_json_string_list(self._values["mcp-args"], "Arguments"),
            env=_optional_json_mapping(self._values["mcp-env"], "Environment"),
            inherit_env=_optional_json_string_list(
                self._values["mcp-inherit-env"], "Inherited environment"
            ),
            cwd=self._values["mcp-cwd"] or None,
            headers=_optional_json_mapping(self._values["mcp-headers"], "Headers"),
            credential_refs=_optional_json_mapping(
                self._values["mcp-credential-refs"], "Credential references"
            ),
            enabled=self._toggles["mcp-enabled"],
            auto_start=self._toggles["mcp-auto-start"],
            auto_reconnect=self._toggles["mcp-auto-reconnect"],
            max_reconnect_attempts=_non_negative_int(
                self._values["mcp-reconnect-attempts"], "Reconnect attempts"
            ),
            reconnect_backoff_seconds=_positive_float(
                self._values["mcp-reconnect-backoff"], "Retry delay"
            ),
            timeout_seconds=_positive_float(self._values["mcp-timeout"], "Timeout"),
            health_check_interval_seconds=_positive_float(
                self._values["mcp-health-interval"], "Connection check interval"
            ),
            permission_mode=cast(PermissionMode, self._values["mcp-permission"]),
            tool_effect_overrides=(
                cast(dict[str, ToolEffect], effects) if effects is not None else None
            ),
        )

    def _validation_error(self) -> str:
        try:
            self._draft()
        except ValueError as exc:
            return str(exc)
        return ""

    def _mcp_choices(self) -> tuple[Choice, ...]:
        error = self._validation_error()
        existing = self._existing
        preserved = theme_for(self).inline_separator.join(("Configured", "preserved"))
        return (
            Choice(
                "mcp-name",
                "Display name",
                trailing=self._values["mcp-name"] or "Required",
                group="DETAILS",
                edit_value=self._values["mcp-name"],
                edit_placeholder="Enter a display name",
            ),
            Choice(
                "mcp-transport",
                "Connection type",
                trailing={"stdio": "Local process", "sse": "Remote (SSE)", "http": "Remote (HTTP)"}[
                    self._values["mcp-transport"]
                ],
                group="CONNECTION",
            ),
            Choice(
                "mcp-target",
                "Command" if self._values["mcp-transport"] == "stdio" else "URL",
                trailing=self._values["mcp-target"] or "Required",
                group="CONNECTION",
                edit_value=self._values["mcp-target"],
                edit_placeholder=(
                    "Enter executable only (e.g. npx); put arguments in Arguments (JSON)"
                    if self._values["mcp-transport"] == "stdio"
                    else "Enter the MCP URL"
                ),
            ),
            Choice(
                "mcp-args",
                "Arguments (JSON)",
                trailing=preserved
                if existing is not None and existing.args and not self._values["mcp-args"]
                else self._values["mcp-args"] or "Default",
                group="PROCESS",
                edit_value=self._values["mcp-args"],
                edit_placeholder='Enter a JSON array, e.g. ["-m", "server"]',
            ),
            Choice(
                "mcp-cwd",
                "Working directory",
                trailing=self._values["mcp-cwd"] or "Default",
                group="PROCESS",
                edit_value=self._values["mcp-cwd"],
                edit_placeholder="Enter a working directory or leave blank",
            ),
            Choice(
                "mcp-inherit-env",
                "Inherited env names (JSON)",
                trailing=preserved
                if existing is not None
                and existing.inherit_env
                and not self._values["mcp-inherit-env"]
                else self._values["mcp-inherit-env"] or "Default",
                group="PROCESS",
                edit_value=self._values["mcp-inherit-env"],
                edit_placeholder='Enter a JSON array, e.g. ["PATH"]',
            ),
            Choice(
                "mcp-env",
                "Environment (JSON)",
                trailing=(
                    "Configured"
                    if self._values["mcp-env"]
                    else preserved
                    if existing is not None and existing.env
                    else "Not set"
                ),
                group="CREDENTIALS",
                edit_value=self._values["mcp-env"],
                edit_placeholder='Enter a JSON object, e.g. {"KEY":"value"}',
            ),
            Choice(
                "mcp-headers",
                "HTTP headers (JSON)",
                trailing=(
                    "Configured"
                    if self._values["mcp-headers"]
                    else preserved
                    if existing is not None and existing.headers
                    else "Not set"
                ),
                group="CREDENTIALS",
                edit_value=self._values["mcp-headers"],
                edit_placeholder='Enter a JSON object, e.g. {"Authorization":"..."}',
            ),
            Choice(
                "mcp-credential-refs",
                "Credential references (JSON)",
                trailing=(
                    "Configured"
                    if self._values["mcp-credential-refs"]
                    else preserved
                    if existing is not None and existing.credential_refs
                    else "Not set"
                ),
                group="CREDENTIALS",
                edit_value=self._values["mcp-credential-refs"],
                edit_placeholder="Enter credential references as a JSON object",
            ),
            Choice(
                "mcp-permission",
                "Default tool permission",
                trailing=PERMISSION_LABELS[self._values["mcp-permission"]],
                group="TOOLS",
            ),
            Choice(
                "mcp-tool-effects",
                "Tool effect overrides (JSON)",
                trailing=preserved
                if existing is not None
                and existing.tool_effect_overrides
                and not self._values["mcp-tool-effects"]
                else self._values["mcp-tool-effects"] or "Default",
                group="TOOLS",
                edit_value=self._values["mcp-tool-effects"],
                edit_placeholder="Enter tool effects as a JSON object",
            ),
            Choice(
                "mcp-enabled",
                "Enabled",
                trailing=_enabled_label(self._toggles["mcp-enabled"]),
                group="CONNECTION SETTINGS",
            ),
            Choice(
                "mcp-auto-start",
                "Auto start",
                trailing=_enabled_label(self._toggles["mcp-auto-start"]),
                group="CONNECTION SETTINGS",
            ),
            Choice(
                "mcp-auto-reconnect",
                "Auto reconnect",
                trailing=_enabled_label(self._toggles["mcp-auto-reconnect"]),
                group="CONNECTION SETTINGS",
            ),
            Choice(
                "mcp-reconnect-attempts",
                "Reconnect attempts",
                trailing=self._values["mcp-reconnect-attempts"],
                group="CONNECTION SETTINGS",
                edit_value=self._values["mcp-reconnect-attempts"],
                edit_placeholder="Enter zero or a positive integer",
            ),
            Choice(
                "mcp-reconnect-backoff",
                "Retry delay",
                trailing=f"{self._values['mcp-reconnect-backoff']} seconds",
                group="CONNECTION SETTINGS",
                edit_value=self._values["mcp-reconnect-backoff"],
                edit_placeholder="Enter seconds",
            ),
            Choice(
                "mcp-timeout",
                "Timeout",
                trailing=f"{self._values['mcp-timeout']} seconds",
                group="CONNECTION SETTINGS",
                edit_value=self._values["mcp-timeout"],
                edit_placeholder="Enter seconds",
            ),
            Choice(
                "mcp-health-interval",
                "Connection check interval",
                trailing=f"{self._values['mcp-health-interval']} seconds",
                group="CONNECTION SETTINGS",
                edit_value=self._values["mcp-health-interval"],
                edit_placeholder="Enter seconds",
            ),
            Choice("mcp-save", "Save", trailing=error, group="ACTIONS", disabled=bool(error)),
        )
