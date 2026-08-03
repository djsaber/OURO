from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

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

_RUNTIME_MODEL = "__ouro_runtime_model__"
_NO_FALLBACK = "__ouro_no_fallback__"
_REASONING_VALUES = (
    "default",
    "enabled",
    "disabled",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
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
            yield Label("Summary token budget", classes="configuration-label")
            yield Input(value=str(policy["summary_output_tokens"]), id="compaction-summary-tokens")
            yield Label("Compaction model route", classes="configuration-label")
            yield Select[str](
                tuple(model_options),
                value=configured_model,
                allow_blank=False,
                compact=True,
                id="compaction-model",
            )
            yield Label("Temperature (blank = endpoint default)", classes="configuration-label")
            yield Input(value=_optional_text(config.model.temperature), id="compaction-temperature")
            yield Label(
                "Max output tokens (blank = endpoint default)", classes="configuration-label"
            )
            yield Input(
                value=_optional_text(config.model.max_output_tokens), id="compaction-max-output"
            )
            yield Label("Thinking", classes="configuration-label")
            yield Select[str](
                tuple((item, item) for item in _REASONING_VALUES),
                value=reasoning,
                allow_blank=False,
                compact=True,
                id="compaction-reasoning",
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
            reasoning = _selected(self, "#compaction-reasoning", "Select thinking")
            trigger = _ratio(self.query_one("#compaction-trigger", Input).value, "Trigger ratio")
            target = _ratio(self.query_one("#compaction-target", Input).value, "Target ratio")
            if target >= trigger:
                raise ValueError("Target ratio must be lower than trigger ratio")
            if fallback == strategy:
                raise ValueError("Fallback strategy must differ from the primary strategy")
            draft = TuiCompactionConfiguration(
                strategy_id=strategy,
                fallback_strategy_id=None if fallback == _NO_FALLBACK else fallback,
                auto_enabled=self.query_one("#compaction-auto", Checkbox).value,
                trigger_ratio=trigger,
                target_ratio=target,
                summary_output_tokens=_minimum_int(
                    self.query_one("#compaction-summary-tokens", Input).value,
                    "Summary token budget",
                    minimum=256,
                ),
                model_key=None if model == _RUNTIME_MODEL else model,
                temperature=_optional_bounded_float(
                    self.query_one("#compaction-temperature", Input).value,
                    "Temperature",
                    minimum=0.0,
                    maximum=2.0,
                ),
                max_output_tokens=_optional_positive_int(
                    self.query_one("#compaction-max-output", Input).value,
                    "Max output tokens",
                ),
                reasoning=_reasoning_request(reasoning),
            )
        except ValueError as exc:
            self.query_one(".configuration-error", Static).update(str(exc))
            return
        self.dismiss(draft)


class MCPConfigurationScreen(ModalScreen[TuiMCPServerConfiguration | None]):
    """Collect MCP configuration while preserving redacted fields on edits."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("ctrl+c", "cancel", "Close", show=False, priority=True),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    def __init__(self, existing: MCPServerConfigView | None = None) -> None:
        super().__init__()
        self._existing = existing

    def compose(self) -> ComposeResult:
        existing = self._existing
        transport = existing.transport if existing is not None else "stdio"
        target = existing.command if existing is not None and transport == "stdio" else ""
        with VerticalScroll(classes="configuration-panel mcp-configuration-panel"):
            yield Static(
                "EDIT MCP SERVER" if existing is not None else "ADD MCP SERVER",
                classes="configuration-title",
            )
            yield Label("Server ID", classes="configuration-label")
            yield Input(
                value=existing.server_id if existing is not None else "",
                placeholder="e.g. filesystem",
                disabled=existing is not None,
                id="mcp-server-id",
            )
            yield Label("Display name", classes="configuration-label")
            yield Input(
                value=existing.name if existing is not None else "",
                id="mcp-name",
            )
            yield Label("Transport", classes="configuration-label")
            yield Select[str](
                (("stdio", "stdio"), ("Streamable HTTP", "http"), ("SSE", "sse")),
                value=transport,
                allow_blank=False,
                compact=True,
                id="mcp-transport",
            )
            yield Label("Command / URL", classes="configuration-label")
            yield Input(
                value=target or "",
                placeholder=(
                    "Leave blank to preserve the configured target"
                    if existing is not None
                    else "Command for stdio, URL for HTTP/SSE"
                ),
                id="mcp-target",
            )
            yield Label("Arguments (JSON string array)", classes="configuration-label")
            yield Input(
                placeholder='Blank preserves existing values; e.g. ["-m", "server"]',
                id="mcp-args",
            )
            yield Label("Working directory", classes="configuration-label")
            yield Input(
                value=existing.cwd or "" if existing is not None else "",
                placeholder="Blank preserves existing value",
                id="mcp-cwd",
            )
            yield Label("Environment (JSON object, masked)", classes="configuration-label")
            yield Input(
                placeholder="Blank preserves existing values",
                password=True,
                id="mcp-env",
            )
            yield Label("Inherited env names (JSON string array)", classes="configuration-label")
            yield Input(
                placeholder="Blank preserves defaults/existing values",
                id="mcp-inherit-env",
            )
            yield Label("HTTP headers (JSON object, masked)", classes="configuration-label")
            yield Input(
                placeholder="Blank preserves existing values",
                password=True,
                id="mcp-headers",
            )
            yield Label(
                "Credential references (JSON object, masked)",
                classes="configuration-label",
            )
            yield Input(
                placeholder='e.g. {"Authorization":"env:OURO_MCP_TOKEN"}',
                password=True,
                id="mcp-credential-refs",
            )
            yield Label("Default tool permission", classes="configuration-label")
            yield Select[str](
                (("Auto", "auto"), ("Approval", "approval"), ("Deny", "deny")),
                value=existing.permission_mode if existing is not None else "approval",
                allow_blank=False,
                compact=True,
                id="mcp-permission",
            )
            yield Label("Tool effect overrides (JSON object)", classes="configuration-label")
            yield Input(
                placeholder='e.g. {"read":"read_only","write":"state_changing"}',
                id="mcp-tool-effects",
            )
            with Horizontal(classes="configuration-checks"):
                yield Checkbox(
                    "Enabled",
                    value=existing.enabled if existing is not None else True,
                    id="mcp-enabled",
                )
                yield Checkbox(
                    "Auto start",
                    value=existing.auto_start if existing is not None else False,
                    id="mcp-auto-start",
                )
                yield Checkbox(
                    "Auto reconnect",
                    value=existing.auto_reconnect if existing is not None else True,
                    id="mcp-auto-reconnect",
                )
            with Horizontal(classes="configuration-number-row"):
                with Vertical(classes="configuration-number-field"):
                    yield Label("Reconnect attempts", classes="configuration-label")
                    yield Input(
                        value=str(existing.max_reconnect_attempts if existing else 3),
                        id="mcp-reconnect-attempts",
                    )
                with Vertical(classes="configuration-number-field"):
                    yield Label("Reconnect backoff", classes="configuration-label")
                    yield Input(
                        value=str(existing.reconnect_backoff_seconds if existing else 0.5),
                        id="mcp-reconnect-backoff",
                    )
            with Horizontal(classes="configuration-number-row"):
                with Vertical(classes="configuration-number-field"):
                    yield Label("Timeout seconds", classes="configuration-label")
                    yield Input(
                        value=str(existing.timeout_seconds if existing else 30.0),
                        id="mcp-timeout",
                    )
                with Vertical(classes="configuration-number-field"):
                    yield Label("Health interval", classes="configuration-label")
                    yield Input(
                        value=str(existing.health_check_interval_seconds if existing else 30.0),
                        id="mcp-health-interval",
                    )
            yield Static(
                "Masked fields are write-only. Blank values preserve existing configuration.",
                classes="configuration-note",
            )
            yield Static("", classes="configuration-error")
            yield Static(
                "Tab move · Space toggle · Ctrl+S save · Esc cancel",
                classes="configuration-hint",
            )
            with Horizontal(classes="configuration-actions"):
                yield Button("Cancel", id="mcp-config-cancel")
                yield Button("Save", variant="primary", id="mcp-config-save")

    def on_mount(self) -> None:
        selector = "#mcp-name" if self._existing is not None else "#mcp-server-id"
        self.query_one(selector, Input).focus()

    @on(Button.Pressed, "#mcp-config-cancel")
    def cancel_from_button(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#mcp-config-save")
    def save_from_button(self) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        self._submit()

    def _submit(self) -> None:
        try:
            server_id = self.query_one("#mcp-server-id", Input).value.strip()
            name = self.query_one("#mcp-name", Input).value.strip()
            transport_value = self.query_one("#mcp-transport", Select).value
            permission_value = self.query_one("#mcp-permission", Select).value
            if not server_id:
                raise ValueError("Server ID is required")
            if not name:
                raise ValueError("Display name is required")
            if transport_value not in {"stdio", "http", "sse"}:
                raise ValueError("Select an MCP transport")
            if permission_value not in {"auto", "approval", "deny"}:
                raise ValueError("Select a tool permission")
            transport = cast(MCPTransportInput, transport_value)
            target = self.query_one("#mcp-target", Input).value.strip() or None
            if (self._existing is None or transport != self._existing.transport) and not target:
                raise ValueError("Command or URL is required for a new transport")
            effects = _optional_json_mapping(
                self.query_one("#mcp-tool-effects", Input).value,
                "Tool effect overrides",
            )
            if effects is not None and any(
                value not in {"read_only", "state_changing", "unknown"}
                for value in effects.values()
            ):
                raise ValueError("Tool effects must be read_only, state_changing, or unknown")
            draft = TuiMCPServerConfiguration(
                server_id=server_id,
                name=name,
                transport=transport,
                target=target,
                args=_optional_json_string_list(
                    self.query_one("#mcp-args", Input).value,
                    "Arguments",
                ),
                env=_optional_json_mapping(
                    self.query_one("#mcp-env", Input).value,
                    "Environment",
                ),
                inherit_env=_optional_json_string_list(
                    self.query_one("#mcp-inherit-env", Input).value,
                    "Inherited environment",
                ),
                cwd=self.query_one("#mcp-cwd", Input).value.strip() or None,
                headers=_optional_json_mapping(
                    self.query_one("#mcp-headers", Input).value,
                    "Headers",
                ),
                credential_refs=_optional_json_mapping(
                    self.query_one("#mcp-credential-refs", Input).value,
                    "Credential references",
                ),
                enabled=self.query_one("#mcp-enabled", Checkbox).value,
                auto_start=self.query_one("#mcp-auto-start", Checkbox).value,
                auto_reconnect=self.query_one("#mcp-auto-reconnect", Checkbox).value,
                max_reconnect_attempts=_non_negative_int(
                    self.query_one("#mcp-reconnect-attempts", Input).value,
                    "Reconnect attempts",
                ),
                reconnect_backoff_seconds=_positive_float(
                    self.query_one("#mcp-reconnect-backoff", Input).value,
                    "Reconnect backoff",
                ),
                timeout_seconds=_positive_float(
                    self.query_one("#mcp-timeout", Input).value,
                    "Timeout",
                ),
                health_check_interval_seconds=_positive_float(
                    self.query_one("#mcp-health-interval", Input).value,
                    "Health interval",
                ),
                permission_mode=cast(PermissionMode, permission_value),
                tool_effect_overrides=(
                    cast(dict[str, ToolEffect], effects) if effects is not None else None
                ),
            )
        except ValueError as exc:
            self.query_one(".configuration-error", Static).update(str(exc))
            return
        self.dismiss(draft)


class SkillInstallScreen(ModalScreen[TuiSkillInstallConfiguration | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("ctrl+c", "cancel", "Close", show=False, priority=True),
        Binding("ctrl+s", "save", "Install", show=False, priority=True),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="configuration-panel"):
            yield Static("INSTALL SKILL", classes="configuration-title")
            yield Label("Source", classes="configuration-label")
            yield Input(
                placeholder="Local path, Git URL, or github:owner/repo",
                id="skill-source",
            )
            yield Label("Scope", classes="configuration-label")
            yield Select[str](
                (("User", "user"), ("Workspace", "workspace")),
                value="user",
                allow_blank=False,
                compact=True,
                id="skill-scope",
            )
            yield Label("Skill selector", classes="configuration-label")
            yield Input(placeholder="Optional name within a collection", id="skill-selector")
            yield Label("Git ref", classes="configuration-label")
            yield Input(placeholder="Optional branch or tag", id="skill-ref")
            with Horizontal(classes="configuration-checks"):
                yield Checkbox("Install all", id="skill-install-all")
                yield Checkbox("Replace existing", id="skill-force")
            yield Static("", classes="configuration-error")
            yield Static(
                "Tab move · Space toggle · Ctrl+S install · Esc cancel",
                classes="configuration-hint",
            )
            with Horizontal(classes="configuration-actions"):
                yield Button("Cancel", id="skill-install-cancel")
                yield Button("Install", variant="primary", id="skill-install-save")

    def on_mount(self) -> None:
        self.query_one("#skill-source", Input).focus()

    @on(Button.Pressed, "#skill-install-cancel")
    def cancel_from_button(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#skill-install-save")
    def save_from_button(self) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        self._submit()

    def _submit(self) -> None:
        source = self.query_one("#skill-source", Input).value.strip()
        scope = self.query_one("#skill-scope", Select).value
        selector = self.query_one("#skill-selector", Input).value.strip() or None
        install_all = self.query_one("#skill-install-all", Checkbox).value
        if not source:
            self.query_one(".configuration-error", Static).update("Source is required")
            return
        if scope not in {"user", "workspace"}:
            self.query_one(".configuration-error", Static).update("Select an install scope")
            return
        if selector is not None and install_all:
            self.query_one(".configuration-error", Static).update(
                "Skill selector and Install all are mutually exclusive"
            )
            return
        self.dismiss(
            TuiSkillInstallConfiguration(
                source=source,
                scope=cast(SkillInstallScopeInput, scope),
                selector=selector,
                install_all=install_all,
                force=self.query_one("#skill-force", Checkbox).value,
                ref=self.query_one("#skill-ref", Input).value.strip() or None,
            )
        )


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Confirm", show=False),
        Binding("n,escape,ctrl+c", "cancel", "Cancel", show=False, priority=True),
    ]

    def __init__(self, *, title: str, message: str, confirm_label: str = "Confirm") -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="confirm-panel"):
            yield Static(self._title, classes="configuration-title")
            yield Static(self._message, classes="configuration-note")
            yield Static("Y confirm · N/Esc cancel", classes="configuration-hint")
            with Horizontal(classes="configuration-actions"):
                yield Button("Cancel", id="confirm-cancel")
                yield Button(self._confirm_label, variant="error", id="confirm-accept")

    def on_mount(self) -> None:
        self.query_one("#confirm-cancel", Button).focus()

    @on(Button.Pressed, "#confirm-cancel")
    def cancel_from_button(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#confirm-accept")
    def confirm_from_button(self) -> None:
        self.action_confirm()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)


def _optional_json_string_list(raw: str, label: str) -> list[str] | None:
    value = _optional_json(raw, label)
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a JSON string array")
    return value


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


def _minimum_int(raw: str, label: str, *, minimum: int) -> int:
    value = _integer(raw, label)
    if value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
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
