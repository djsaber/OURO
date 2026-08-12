from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from ouro.application.configuration_models import (
    CompactionConfigView,
    MCPServerConfigView,
    MCPTransportInput,
    SkillInstallScopeInput,
    ToolExecutionConfigView,
)
from ouro.application.models import AvailableModel
from ouro.core.reasoning import ReasoningRequest
from ouro.core.tool_batches import ToolExecutionMode
from ouro.core.tools import PermissionMode, ToolEffect
from ouro.tui.controller import (
    TuiCompactionConfiguration,
    TuiMCPServerConfiguration,
    TuiSkillInstallConfiguration,
    TuiToolExecutionConfiguration,
)
from ouro.tui.model_menus import model_choices
from ouro.tui.screens import Choice, ChoiceScreen, EditableChoiceScreen
from ouro.tui.theme import SELECTED_CHOICE_MARKER

_RUNTIME_MODEL = "__ouro_runtime_model__"
_NO_FALLBACK = "__ouro_no_fallback__"
_REASONING_REQUESTS: dict[str, ReasoningRequest | None] = {
    "default": None,
    "enabled": ReasoningRequest(mode="enabled"),
    "disabled": ReasoningRequest(mode="disabled"),
    "minimal": ReasoningRequest(mode="enabled", effort="minimal"),
    "low": ReasoningRequest(mode="enabled", effort="low"),
    "medium": ReasoningRequest(mode="enabled", effort="medium"),
    "high": ReasoningRequest(mode="enabled", effort="high"),
    "xhigh": ReasoningRequest(mode="enabled", effort="xhigh"),
    "max": ReasoningRequest(mode="enabled", effort="max"),
}


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
            title="TOOL EXECUTION & PATH ACCESS",
            placeholder="Filter tool execution settings…",
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
                self._values["tool-max-concurrency"], "Max concurrency"
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
        except ValueError as exc:
            self.notify(str(exc), severity="warning", markup=False)
            return
        self.dismiss(draft)  # type: ignore[arg-type]

    def _tool_execution_choices(self) -> tuple[Choice, ...]:
        error = self._validation_error()
        return (
            Choice(
                "tool-execution-mode",
                "Execution mode",
                trailing=self._values["tool-execution-mode"],
                group="EXECUTION",
            ),
            Choice(
                "tool-max-concurrency",
                "Max concurrency",
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


class CompactionConfigurationScreen(EditableChoiceScreen):
    """Edit compaction policy through the shared grouped inline menu."""

    def __init__(
        self,
        config: CompactionConfigView,
        *,
        models: tuple[AvailableModel, ...],
        strategy_ids: tuple[str, ...],
    ) -> None:
        self._models = models
        self._strategy_ids = tuple(
            dict.fromkeys(
                (
                    config.strategy_id,
                    *((config.fallback_strategy_id,) if config.fallback_strategy_id else ()),
                    *(strategy_ids or (config.strategy_id,)),
                )
            )
        )
        policy = config.policy
        configured_model = (
            f"{config.model.provider_id}/{config.model.endpoint_id}"
            if config.model.has_dedicated_endpoint
            else _RUNTIME_MODEL
        )
        self._model_labels = {
            _RUNTIME_MODEL: "Session runtime model",
            **{model.key: model.display_name or model.key for model in models},
        }
        if configured_model not in self._model_labels:
            self._model_labels[configured_model] = f"Unavailable · {configured_model}"
        reasoning = _reasoning_value(config.model.reasoning)
        configured_capability = self._model(configured_model)
        reasoning_options = _compaction_reasoning_options(configured_capability)
        if reasoning not in reasoning_options:
            reasoning = "default"
        self._values = {
            "compaction-strategy": config.strategy_id,
            "compaction-fallback": config.fallback_strategy_id or _NO_FALLBACK,
            "compaction-trigger": str(policy["trigger_ratio"]),
            "compaction-target": str(policy["target_ratio"]),
            "compaction-model": configured_model,
            "compaction-temperature": _optional_text(config.model.temperature),
            "compaction-max-output": _optional_text(config.model.max_output_tokens),
            "compaction-reasoning": reasoning,
        }
        self._auto_enabled = bool(policy["auto_enabled"])
        super().__init__(
            title="CONTEXT COMPACTION",
            placeholder="Filter compaction settings…",
            choices=self._compaction_choices(),
            on_save=self._save_field,
        )

    async def _select_choice(self, selected: str) -> None:
        if self._editing_value is not None:
            await super()._select_choice(selected)
            return
        if selected == "compaction-auto":
            self._auto_enabled = not self._auto_enabled
            self._refresh(selected)
            return
        if selected == "compaction-model":
            model = await self.open_child(
                ChoiceScreen(
                    title="COMPACTION MODEL",
                    placeholder="Filter models…",
                    choices=self._model_choices(),
                    initial_value=self._values["compaction-model"],
                )
            )
            self._apply_model_selection(model)
            return
        cycles = {
            "compaction-strategy": self._strategy_ids,
            "compaction-fallback": (_NO_FALLBACK, *self._strategy_ids),
            "compaction-reasoning": _compaction_reasoning_options(
                self._model(self._values["compaction-model"])
            ),
        }
        if selected in cycles:
            values = cycles[selected]
            current = self._values[selected]
            self._values[selected] = values[(values.index(current) + 1) % len(values)]
            self._refresh(selected)
            return
        if selected == "compaction-save":
            self._submit()
            return
        await super()._select_choice(selected)

    async def _save_field(self, field_id: str, value: str) -> tuple[Choice, ...] | None:
        self._values[field_id] = value.strip()
        return self._compaction_choices()

    def _refresh(self, selected: str) -> None:
        self._replace_choices(self._compaction_choices(), selected=selected)

    def _normalize_model_options(self) -> None:
        model_key = self._values["compaction-model"]
        if model_key == _RUNTIME_MODEL:
            self._values["compaction-temperature"] = ""
            self._values["compaction-max-output"] = ""
        reasoning_options = _compaction_reasoning_options(self._model(model_key))
        if self._values["compaction-reasoning"] not in reasoning_options:
            self._values["compaction-reasoning"] = "default"

    def _apply_model_selection(self, selected: str | None) -> None:
        if selected is None:
            return
        self._values["compaction-model"] = selected
        self._normalize_model_options()
        self._refresh("compaction-model")

    def _model_choices(self) -> tuple[Choice, ...]:
        selected = self._values["compaction-model"]
        choices = [
            Choice(
                _RUNTIME_MODEL,
                "Session runtime model",
                marker=SELECTED_CHOICE_MARKER if selected == _RUNTIME_MODEL else "",
                group="SESSION",
            )
        ]
        choices.extend(model_choices(self._models, selected=selected))
        if selected != _RUNTIME_MODEL and self._model(selected) is None:
            choices.append(
                Choice(
                    selected,
                    self._model_labels[selected],
                    marker=SELECTED_CHOICE_MARKER,
                    group="UNAVAILABLE",
                    disabled=True,
                )
            )
        return tuple(choices)

    def _submit(self) -> None:
        try:
            draft = self._draft()
        except ValueError as exc:
            self.notify(str(exc), severity="warning", markup=False)
            return
        self.dismiss(draft)  # type: ignore[arg-type]

    def _draft(self) -> TuiCompactionConfiguration:
        strategy = self._values["compaction-strategy"]
        fallback = self._values["compaction-fallback"]
        model = self._values["compaction-model"]
        trigger = _ratio(self._values["compaction-trigger"], "Trigger ratio")
        target = _ratio(self._values["compaction-target"], "Target ratio")
        if target >= trigger:
            raise ValueError("Target ratio must be lower than trigger ratio")
        if fallback == strategy:
            raise ValueError("Fallback strategy must differ from the primary strategy")
        configured = self._model(model)
        max_output_tokens = (
            None
            if model == _RUNTIME_MODEL
            else _optional_positive_int(
                self._values["compaction-max-output"],
                "Max output tokens",
            )
        )
        if (
            configured is not None
            and max_output_tokens is not None
            and max_output_tokens > configured.max_output_tokens
        ):
            raise ValueError(
                "Max output tokens must not exceed the selected endpoint output limit "
                f"({configured.max_output_tokens:,})"
            )
        return TuiCompactionConfiguration(
            strategy_id=strategy,
            fallback_strategy_id=None if fallback == _NO_FALLBACK else fallback,
            auto_enabled=self._auto_enabled,
            trigger_ratio=trigger,
            target_ratio=target,
            model_key=None if model == _RUNTIME_MODEL else model,
            temperature=(
                None
                if model == _RUNTIME_MODEL
                else _optional_bounded_float(
                    self._values["compaction-temperature"],
                    "Temperature",
                    minimum=0.0,
                    maximum=2.0,
                )
            ),
            max_output_tokens=max_output_tokens,
            reasoning=(
                None
                if model == _RUNTIME_MODEL
                else _reasoning_request(self._values["compaction-reasoning"])
            ),
        )

    def _validation_error(self) -> str:
        try:
            self._draft()
        except ValueError as exc:
            return str(exc)
        return ""

    def _compaction_choices(self) -> tuple[Choice, ...]:
        model_key = self._values["compaction-model"]
        configured = self._model(model_key)
        uses_runtime_model = model_key == _RUNTIME_MODEL
        request_options_disabled = uses_runtime_model or configured is None
        reasoning_disabled = (
            request_options_disabled
            or configured is not None
            and configured.reasoning.activation == "unsupported"
        )
        error = self._validation_error()
        fallback = self._values["compaction-fallback"]
        return (
            Choice(
                "compaction-strategy",
                "Primary strategy",
                trailing=self._values["compaction-strategy"],
                group="STRATEGY",
            ),
            Choice(
                "compaction-fallback",
                "Fallback strategy",
                trailing="None" if fallback == _NO_FALLBACK else fallback,
                group="STRATEGY",
            ),
            Choice(
                "compaction-auto",
                "Automatic compaction",
                trailing=_enabled_label(self._auto_enabled),
                group="STRATEGY",
            ),
            Choice(
                "compaction-trigger",
                "Trigger ratio",
                trailing=self._values["compaction-trigger"] or "Required",
                group="THRESHOLDS",
                edit_value=self._values["compaction-trigger"],
                edit_placeholder="Enter a ratio from 0 to 1",
            ),
            Choice(
                "compaction-target",
                "Target ratio",
                trailing=self._values["compaction-target"] or "Required",
                group="THRESHOLDS",
                edit_value=self._values["compaction-target"],
                edit_placeholder="Enter a ratio below the trigger",
            ),
            Choice(
                "compaction-model",
                "Model route",
                trailing=self._model_labels[model_key],
                group="MODEL",
            ),
            Choice(
                "compaction-temperature",
                "Temperature",
                trailing=(
                    "Session model"
                    if uses_runtime_model
                    else self._values["compaction-temperature"] or "Endpoint default"
                ),
                group="MODEL",
                disabled=request_options_disabled,
                edit_value=self._values["compaction-temperature"],
                edit_placeholder="Leave blank to use the endpoint default",
            ),
            Choice(
                "compaction-max-output",
                "Max output tokens",
                trailing=(
                    "Session model"
                    if uses_runtime_model
                    else self._values["compaction-max-output"] or "Endpoint default"
                ),
                group="MODEL",
                disabled=request_options_disabled,
                edit_value=self._values["compaction-max-output"],
                edit_placeholder="Leave blank to use the endpoint default",
            ),
            Choice(
                "compaction-reasoning",
                "Thinking",
                trailing=(
                    "Session model"
                    if uses_runtime_model
                    else "Unsupported"
                    if reasoning_disabled
                    else self._values["compaction-reasoning"]
                ),
                group="MODEL",
                disabled=reasoning_disabled,
            ),
            Choice(
                "compaction-save",
                "Save",
                trailing=error,
                group="ACTIONS",
                disabled=bool(error),
            ),
        )

    def _model(self, key: str) -> AvailableModel | None:
        return next((model for model in self._models if model.key == key), None)


class MCPConfigurationScreen(EditableChoiceScreen):
    """Edit MCP settings through the shared grouped inline-edit menu."""

    _TRANSPORTS = ("stdio", "http", "sse")
    _PERMISSIONS = ("auto", "approval", "deny")
    _TOGGLES = frozenset({"mcp-enabled", "mcp-auto-start", "mcp-auto-reconnect"})
    _MASKED = frozenset({"mcp-env", "mcp-headers", "mcp-credential-refs"})

    def __init__(self, existing: MCPServerConfigView | None = None) -> None:
        self._existing = existing
        transport = existing.transport if existing is not None else "stdio"
        self._values = {
            "mcp-server-id": existing.server_id if existing is not None else "",
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
            title="EDIT MCP SERVER" if existing is not None else "ADD MCP SERVER",
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
        return "•" * len(value) if self._editing_value in self._MASKED else value

    def _refresh(self, selected: str) -> None:
        self._replace_choices(self._mcp_choices(), selected=selected)

    def _submit(self) -> None:
        try:
            draft = self._draft()
        except ValueError as exc:
            self.notify(str(exc), severity="warning", markup=False)
            return
        self.dismiss(draft)  # type: ignore[arg-type]

    def _draft(self) -> TuiMCPServerConfiguration:
        server_id = self._values["mcp-server-id"]
        name = self._values["mcp-name"]
        if not server_id:
            raise ValueError("Server ID is required")
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
            server_id=server_id,
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
                self._values["mcp-reconnect-backoff"], "Reconnect backoff"
            ),
            timeout_seconds=_positive_float(self._values["mcp-timeout"], "Timeout"),
            health_check_interval_seconds=_positive_float(
                self._values["mcp-health-interval"], "Health interval"
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
        preserved = "Configured · preserved"
        return (
            Choice(
                "mcp-server-id",
                "Server ID",
                trailing=self._values["mcp-server-id"] or "Required",
                group="IDENTITY",
                disabled=existing is not None,
                edit_value=self._values["mcp-server-id"],
                edit_placeholder="Enter a unique server ID",
            ),
            Choice(
                "mcp-name",
                "Display name",
                trailing=self._values["mcp-name"] or "Required",
                group="IDENTITY",
                edit_value=self._values["mcp-name"],
                edit_placeholder="Enter a display name",
            ),
            Choice(
                "mcp-transport",
                "Transport",
                trailing=self._values["mcp-transport"],
                group="CONNECTION",
            ),
            Choice(
                "mcp-target",
                "Command / URL",
                trailing=self._values["mcp-target"] or "Required",
                group="CONNECTION",
                edit_value=self._values["mcp-target"],
                edit_placeholder="Enter a command or URL",
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
                edit_placeholder='Enter credential references as a JSON object',
            ),
            Choice(
                "mcp-permission",
                "Default tool permission",
                trailing=self._values["mcp-permission"],
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
                edit_placeholder='Enter tool effects as a JSON object',
            ),
            Choice(
                "mcp-enabled",
                "Enabled",
                trailing=_enabled_label(self._toggles["mcp-enabled"]),
                group="RUNTIME",
            ),
            Choice(
                "mcp-auto-start",
                "Auto start",
                trailing=_enabled_label(self._toggles["mcp-auto-start"]),
                group="RUNTIME",
            ),
            Choice(
                "mcp-auto-reconnect",
                "Auto reconnect",
                trailing=_enabled_label(self._toggles["mcp-auto-reconnect"]),
                group="RUNTIME",
            ),
            Choice(
                "mcp-reconnect-attempts",
                "Reconnect attempts",
                trailing=self._values["mcp-reconnect-attempts"],
                group="RUNTIME",
                edit_value=self._values["mcp-reconnect-attempts"],
                edit_placeholder="Enter zero or a positive integer",
            ),
            Choice(
                "mcp-reconnect-backoff",
                "Reconnect backoff",
                trailing=f"{self._values['mcp-reconnect-backoff']} seconds",
                group="RUNTIME",
                edit_value=self._values["mcp-reconnect-backoff"],
                edit_placeholder="Enter seconds",
            ),
            Choice(
                "mcp-timeout",
                "Timeout",
                trailing=f"{self._values['mcp-timeout']} seconds",
                group="RUNTIME",
                edit_value=self._values["mcp-timeout"],
                edit_placeholder="Enter seconds",
            ),
            Choice(
                "mcp-health-interval",
                "Health interval",
                trailing=f"{self._values['mcp-health-interval']} seconds",
                group="RUNTIME",
                edit_value=self._values["mcp-health-interval"],
                edit_placeholder="Enter seconds",
            ),
            Choice("mcp-save", "Save", trailing=error, group="ACTIONS", disabled=bool(error)),
        )


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
        except ValueError as exc:
            self.notify(str(exc), severity="warning", markup=False)
            return
        self.dismiss(draft)  # type: ignore[arg-type]

    def _draft(self) -> TuiSkillInstallConfiguration:
        source = self._values["skill-source"]
        selector = self._values["skill-selector"] or None
        install_all = self._toggles["skill-install-all"]
        if not source:
            raise ValueError("Source is required")
        if selector is not None and install_all:
            raise ValueError("Skill selector and Install all are mutually exclusive")
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
                "Skill selector",
                trailing=self._values["skill-selector"] or "All discovered skills",
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
                "Git ref",
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


def _optional_json_string_list(raw: str, label: str) -> list[str] | None:
    value = _optional_json(raw, label)
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a JSON string array")
    return value


def _enabled_label(enabled: bool) -> str:
    return "On" if enabled else "Off"


def _optional_json_mapping(raw: str, label: str) -> dict[str, str] | None:
    value = _optional_json(raw, label)
    if value is None:
        return None
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError(f"{label} must be a JSON object with string values")
    return dict(value)


def _optional_json(raw: str, label: str) -> object | None:
    normalized = raw.strip()
    if not normalized:
        return None
    try:
        return json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON: {exc.msg}") from exc


def _positive_int(raw: str, label: str) -> int:
    value = _integer(raw, label)
    if value < 1:
        raise ValueError(f"{label} must be positive")
    return value


def _optional_positive_int(raw: str, label: str) -> int | None:
    return _positive_int(raw, label) if raw.strip() else None


def _non_negative_int(raw: str, label: str) -> int:
    value = _integer(raw, label)
    if value < 0:
        raise ValueError(f"{label} must not be negative")
    return value


def _integer(raw: str, label: str) -> int:
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer") from exc


def _positive_float(raw: str, label: str) -> float:
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{label} must be positive")
    return value


def _ratio(raw: str, label: str) -> float:
    value = _positive_float(raw, label)
    if value > 1:
        raise ValueError(f"{label} must not exceed 1")
    return value


def _optional_bounded_float(
    raw: str,
    label: str,
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    if not raw.strip():
        return None
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}")
    return value


def _optional_text(value: object | None) -> str:
    return "" if value is None else str(value)


def _reasoning_value(request: ReasoningRequest | None) -> str:
    if request is None:
        return "default"
    return request.effort or request.mode


def _reasoning_request(value: str) -> ReasoningRequest | None:
    try:
        return _REASONING_REQUESTS[value]
    except KeyError as exc:
        raise ValueError(f"Unsupported thinking option: {value}") from exc


def _compaction_reasoning_options(model: AvailableModel | None) -> tuple[str, ...]:
    if model is None or model.reasoning.activation == "unsupported":
        return ("default",)
    values = ["default", "enabled"]
    if model.reasoning.activation == "toggle":
        values.append("disabled")
    values.extend(model.reasoning.supported_efforts)
    return tuple(values)
