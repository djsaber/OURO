from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, OptionList, Select, Static

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
from ouro.tui.screens import Choice, EditableChoiceScreen

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


class ToolExecutionConfigurationScreen(ModalScreen[TuiToolExecutionConfiguration | None]):
    """Edit tool scheduling and the Host-owned path access boundary."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("ctrl+c", "cancel", "Close", show=False, priority=True),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    def __init__(self, config: ToolExecutionConfigView) -> None:
        super().__init__()
        self._config = config

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="configuration-panel"):
            yield Static("TOOL EXECUTION & PATH ACCESS", classes="configuration-title")
            yield Label("Execution mode", classes="configuration-label")
            yield Select[str](
                (("Sequential", "sequential"), ("Parallel", "parallel")),
                value=self._config.mode,
                allow_blank=False,
                compact=True,
                id="tool-execution-mode",
            )
            yield Label("Max concurrency", classes="configuration-label")
            yield Input(value=str(self._config.max_concurrency), id="tool-max-concurrency")
            yield Label("Max output characters", classes="configuration-label")
            yield Input(value=str(self._config.max_output_chars), id="tool-max-output")
            yield Checkbox(
                "Restrict file and process paths to the workspace",
                value=self._config.workspace_only,
                id="tool-workspace-only",
            )
            yield Static(
                "Tool effects are contracts; path scope does not change read/write labels.",
                classes="configuration-note",
            )
            yield Static("", classes="configuration-error")
            yield Static(
                "Tab move · Space toggle · Ctrl+S save · Esc cancel",
                classes="configuration-hint",
            )
            with Horizontal(classes="configuration-actions"):
                yield Button("Cancel", id="tool-config-cancel")
                yield Button("Save", variant="primary", id="tool-config-save")

    def on_mount(self) -> None:
        self.query_one("#tool-execution-mode", Select).focus()

    @on(Button.Pressed, "#tool-config-cancel")
    def cancel_from_button(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#tool-config-save")
    def save_from_button(self) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        self._submit()

    def _submit(self) -> None:
        try:
            mode = self.query_one("#tool-execution-mode", Select).value
            if mode not in {"sequential", "parallel"}:
                raise ValueError("Select an execution mode")
            draft = TuiToolExecutionConfiguration(
                mode=cast(ToolExecutionMode, mode),
                max_concurrency=_positive_int(
                    self.query_one("#tool-max-concurrency", Input).value,
                    "Max concurrency",
                ),
                workspace_only=self.query_one("#tool-workspace-only", Checkbox).value,
                max_output_chars=_positive_int(
                    self.query_one("#tool-max-output", Input).value,
                    "Max output characters",
                ),
            )
        except ValueError as exc:
            self.query_one(".configuration-error", Static).update(str(exc))
            return
        self.dismiss(draft)


class CompactionConfigurationScreen(ModalScreen[TuiCompactionConfiguration | None]):
    """Edit the complete durable compaction policy and model route."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("ctrl+c", "cancel", "Close", show=False, priority=True),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    def __init__(
        self,
        config: CompactionConfigView,
        *,
        models: tuple[AvailableModel, ...],
        strategy_ids: tuple[str, ...],
    ) -> None:
        super().__init__()
        self._config = config
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

    def compose(self) -> ComposeResult:
        config = self._config
        policy = config.policy
        configured_model = (
            f"{config.model.provider_id}/{config.model.endpoint_id}"
            if config.model.has_dedicated_endpoint
            else _RUNTIME_MODEL
        )
        model_options = [("Session runtime model", _RUNTIME_MODEL)]
        model_options.extend((model.display_name or model.key, model.key) for model in self._models)
        if configured_model != _RUNTIME_MODEL and all(
            value != configured_model for _, value in model_options
        ):
            model_options.append((f"Unavailable · {configured_model}", configured_model))
        fallback_options = [("None", _NO_FALLBACK)]
        fallback_options.extend((item, item) for item in self._strategy_ids)
        reasoning = _reasoning_value(config.model.reasoning)
        configured_capability = self._model(configured_model)
        reasoning_options = _compaction_reasoning_options(configured_capability)
        if reasoning not in reasoning_options:
            reasoning = "default"
        uses_runtime_model = configured_model == _RUNTIME_MODEL
        request_options_disabled = uses_runtime_model or configured_capability is None
        with VerticalScroll(classes="configuration-panel compaction-configuration-panel"):
            yield Static("CONTEXT COMPACTION", classes="configuration-title")
            yield Label("Primary strategy", classes="configuration-label")
            yield Select[str](
                tuple((item, item) for item in self._strategy_ids),
                value=config.strategy_id,
                allow_blank=False,
                compact=True,
                id="compaction-strategy",
            )
            yield Label("Fallback strategy", classes="configuration-label")
            yield Select[str](
                tuple(fallback_options),
                value=config.fallback_strategy_id or _NO_FALLBACK,
                allow_blank=False,
                compact=True,
                id="compaction-fallback",
            )
            yield Checkbox(
                "Automatically compact when the context reaches the trigger",
                value=bool(policy["auto_enabled"]),
                id="compaction-auto",
            )
            yield Label("Trigger ratio (0–1)", classes="configuration-label")
            yield Input(value=str(policy["trigger_ratio"]), id="compaction-trigger")
            yield Label("Target ratio (must be below trigger)", classes="configuration-label")
            yield Input(value=str(policy["target_ratio"]), id="compaction-target")
            yield Label("Compaction model route", classes="configuration-label")
            yield Select[str](
                tuple(model_options),
                value=configured_model,
                allow_blank=False,
                compact=True,
                id="compaction-model",
            )
            yield Label("Temperature (blank = endpoint default)", classes="configuration-label")
            yield Input(
                value=_optional_text(config.model.temperature),
                disabled=request_options_disabled,
                id="compaction-temperature",
            )
            yield Label(
                "Max output tokens (blank = endpoint default)", classes="configuration-label"
            )
            yield Input(
                value=_optional_text(config.model.max_output_tokens),
                disabled=request_options_disabled,
                id="compaction-max-output",
            )
            yield Label("Thinking (dedicated model only)", classes="configuration-label")
            yield Select[str](
                tuple((item, item) for item in reasoning_options),
                value=reasoning,
                allow_blank=False,
                compact=True,
                disabled=(
                    uses_runtime_model
                    or configured_capability is None
                    or configured_capability.reasoning.activation == "unsupported"
                ),
                id="compaction-reasoning",
            )
            yield Static(
                _compaction_model_options_note(configured_model, configured_capability),
                classes="configuration-note",
                id="compaction-model-options-note",
            )
            yield Static(
                "Saved options apply to the next Runtime; /compact uses the current Runtime.",
                classes="configuration-note",
            )
            yield Static("", classes="configuration-error")
            yield Static(
                "Tab move · Space toggle · Ctrl+S save · Esc cancel", classes="configuration-hint"
            )
            with Horizontal(classes="configuration-actions"):
                yield Button("Cancel", id="compaction-cancel")
                yield Button("Save", variant="primary", id="compaction-save")

    def on_mount(self) -> None:
        self.query_one("#compaction-strategy", Select).focus()

    @on(Select.Changed, "#compaction-model")
    def update_model_options(self, event: Select.Changed) -> None:
        model = event.value if isinstance(event.value, str) else _RUNTIME_MODEL
        configured = self._model(model)
        uses_runtime_model = model == _RUNTIME_MODEL
        temperature = self.query_one("#compaction-temperature", Input)
        max_output = self.query_one("#compaction-max-output", Input)
        reasoning = self.query_one("#compaction-reasoning", Select)

        temperature.disabled = uses_runtime_model or configured is None
        max_output.disabled = uses_runtime_model or configured is None
        if uses_runtime_model:
            temperature.value = ""
            max_output.value = ""

        previous_reasoning = reasoning.value if isinstance(reasoning.value, str) else "default"
        reasoning_options = _compaction_reasoning_options(configured)
        reasoning.set_options(tuple((item, item) for item in reasoning_options))
        reasoning.disabled = (
            uses_runtime_model
            or configured is None
            or configured.reasoning.activation == "unsupported"
        )
        if previous_reasoning in reasoning_options and not reasoning.disabled:
            reasoning.value = previous_reasoning
        else:
            reasoning.value = "default"
        self.query_one("#compaction-model-options-note", Static).update(
            _compaction_model_options_note(model, configured)
        )

    @on(Button.Pressed, "#compaction-cancel")
    def cancel_from_button(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#compaction-save")
    def save_from_button(self) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        self._submit()

    def _submit(self) -> None:
        try:
            strategy = _selected(self, "#compaction-strategy", "Select a strategy")
            fallback = _selected(self, "#compaction-fallback", "Select a fallback")
            model = _selected(self, "#compaction-model", "Select a model route")
            reasoning = (
                None
                if model == _RUNTIME_MODEL
                else _reasoning_request(
                    _selected(self, "#compaction-reasoning", "Select thinking")
                )
            )
            trigger = _ratio(self.query_one("#compaction-trigger", Input).value, "Trigger ratio")
            target = _ratio(self.query_one("#compaction-target", Input).value, "Target ratio")
            if target >= trigger:
                raise ValueError("Target ratio must be lower than trigger ratio")
            if fallback == strategy:
                raise ValueError("Fallback strategy must differ from the primary strategy")
            configured = self._model(model)
            max_output_tokens = (
                None
                if model == _RUNTIME_MODEL
                else _optional_positive_int(
                    self.query_one("#compaction-max-output", Input).value,
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
            draft = TuiCompactionConfiguration(
                strategy_id=strategy,
                fallback_strategy_id=None if fallback == _NO_FALLBACK else fallback,
                auto_enabled=self.query_one("#compaction-auto", Checkbox).value,
                trigger_ratio=trigger,
                target_ratio=target,
                model_key=None if model == _RUNTIME_MODEL else model,
                temperature=(
                    None
                    if model == _RUNTIME_MODEL
                    else _optional_bounded_float(
                        self.query_one("#compaction-temperature", Input).value,
                        "Temperature",
                        minimum=0.0,
                        maximum=2.0,
                    )
                ),
                max_output_tokens=max_output_tokens,
                reasoning=reasoning,
            )
        except ValueError as exc:
            self.query_one(".configuration-error", Static).update(str(exc))
            return
        self.dismiss(draft)

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
        self._choices = self._mcp_choices()
        self._refresh_options(
            needle=self.query_one("#choice-filter", Input).value,
            preferred_value=selected,
        )
        self.query_one("#choice-list", OptionList).focus()

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
            ),
            Choice(
                "mcp-name",
                "Display name",
                trailing=self._values["mcp-name"] or "Required",
                group="IDENTITY",
                edit_value=self._values["mcp-name"],
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
            ),
            Choice(
                "mcp-args",
                "Arguments (JSON)",
                trailing=preserved
                if existing is not None and existing.args and not self._values["mcp-args"]
                else self._values["mcp-args"] or "Default",
                group="PROCESS",
                edit_value=self._values["mcp-args"],
            ),
            Choice(
                "mcp-cwd",
                "Working directory",
                trailing=self._values["mcp-cwd"] or "Default",
                group="PROCESS",
                edit_value=self._values["mcp-cwd"],
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
            ),
            Choice(
                "mcp-reconnect-backoff",
                "Reconnect backoff",
                trailing=f"{self._values['mcp-reconnect-backoff']} seconds",
                group="RUNTIME",
                edit_value=self._values["mcp-reconnect-backoff"],
            ),
            Choice(
                "mcp-timeout",
                "Timeout",
                trailing=f"{self._values['mcp-timeout']} seconds",
                group="RUNTIME",
                edit_value=self._values["mcp-timeout"],
            ),
            Choice(
                "mcp-health-interval",
                "Health interval",
                trailing=f"{self._values['mcp-health-interval']} seconds",
                group="RUNTIME",
                edit_value=self._values["mcp-health-interval"],
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
        self._choices = self._skill_choices()
        self._refresh_options(
            needle=self.query_one("#choice-filter", Input).value,
            preferred_value=selected,
        )
        self.query_one("#choice-list", OptionList).focus()

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


def _selected(screen: CompactionConfigurationScreen, selector: str, error: str) -> str:
    value = screen.query_one(selector, Select).value
    if not isinstance(value, str):
        raise ValueError(error)
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


def _compaction_model_options_note(model_key: str, model: AvailableModel | None) -> str:
    if model_key == _RUNTIME_MODEL:
        return (
            "Temperature, output-token limit, and thinking follow the active Session model "
            "and cannot be overridden here."
        )
    if model is None:
        return "The configured endpoint is unavailable; its request capabilities cannot be edited."

    temperature = "provider default" if model.temperature is None else str(model.temperature)
    request_defaults = (
        f"Blank values inherit endpoint defaults (temperature: {temperature}; "
        f"max output: {model.max_output_tokens:,})."
    )
    if model.reasoning.activation == "unsupported":
        return f"{request_defaults} This endpoint does not expose thinking controls."
    efforts = ", ".join(model.reasoning.supported_efforts)
    thinking = (
        f" Available thinking efforts: {efforts}." if efforts else " Thinking supports mode only."
    )
    return request_defaults + thinking
