from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import cast

from pydantic import ValidationError

from ouro.application.agent_models import (
    AgentDefinitionDetail,
    AgentDefinitionView,
    AgentToolOptionView,
)
from ouro.application.configuration_models import (
    AgentRuntimeConfigView,
    CompactionConfigView,
    MCPServerConfigView,
    MCPTransportInput,
    SkillInstallScopeInput,
    ToolExecutionConfigView,
)
from ouro.application.models import AvailableModel
from ouro.application.plugin_models import PluginDetail
from ouro.core.agents import AgentDefinition, ExecutionMode, ToolSelection
from ouro.core.reasoning import ReasoningRequest
from ouro.core.tool_batches import ToolExecutionMode
from ouro.core.tools import PermissionMode, ToolEffect
from ouro.tui.controller import (
    TuiAgentDefinitionConfiguration,
    TuiAgentRuntimeConfiguration,
    TuiCompactionConfiguration,
    TuiMCPServerConfiguration,
    TuiPluginInstallConfiguration,
    TuiSkillInstallConfiguration,
    TuiToolExecutionConfiguration,
)
from ouro.tui.menu_labels import PERMISSION_LABELS, THINKING_LABELS
from ouro.tui.menu_sorting import sorted_agent_definitions
from ouro.tui.model_menus import model_choices
from ouro.tui.screens import Choice, ChoiceScreen, EditableChoiceScreen
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, theme_for

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


class AgentDispatchPolicyScreen(EditableChoiceScreen):
    """Edit the global subagent dispatch ceiling through shared choice pages."""

    _TOGGLE_FIELDS = frozenset(
        {
            "agent-policy-sync",
            "agent-policy-async",
            "agent-policy-write",
            "agent-policy-network",
            "agent-policy-mcp",
            "agent-policy-external-paths",
        }
    )

    def __init__(
        self,
        config: AgentRuntimeConfigView,
        *,
        definitions: tuple[AgentDefinitionView, ...],
        models: tuple[AvailableModel, ...],
        theme: TuiTheme = DEFAULT_TUI_THEME,
    ) -> None:
        self._initial_tui_theme = theme
        policy = config.spawn_policy
        self._revision = config.revision
        self._definitions = definitions
        self._models = models
        self._allowed_agent_ids = {
            definition.agent_id
            for definition in definitions
            if policy.allows_agent(definition.agent_id)
        }
        self._allowed_model_keys = {model.key for model in models if policy.allows_model(model.key)}
        self._toggles = {
            "agent-policy-sync": "sync" in policy.allowed_execution_modes,
            "agent-policy-async": "async" in policy.allowed_execution_modes,
            "agent-policy-write": policy.allow_write,
            "agent-policy-network": policy.allow_network,
            "agent-policy-mcp": policy.allow_mcp,
            "agent-policy-external-paths": policy.allow_external_paths,
        }
        super().__init__(
            title="SUBAGENT PERMISSIONS",
            placeholder="Search subagent permissions…",
            choices=self._policy_choices(),
            on_save=self._save_field,
        )

    async def _select_choice(self, selected: str) -> None:
        if self._editing_value is not None:
            await super()._select_choice(selected)
            return
        if selected == "agent-policy-agents":
            await self._choose_allowed_agents()
            return
        if selected == "agent-policy-models":
            await self._choose_allowed_models()
            return
        if selected in self._TOGGLE_FIELDS:
            self._toggles[selected] = not self._toggles[selected]
            self._refresh(selected)
            return
        if selected == "agent-policy-save":
            self._submit()
            return
        await super()._select_choice(selected)

    async def _choose_allowed_agents(self) -> None:
        async def toggle(agent_id: str) -> tuple[Choice, ...] | None:
            _toggle_member(self._allowed_agent_ids, agent_id)
            return self._agent_choices()

        await self.open_child(
            ChoiceScreen(
                title="ALLOWED SUBAGENTS",
                placeholder="Filter subagents…",
                choices=self._agent_choices(),
                on_select=toggle,
            )
        )
        self._refresh("agent-policy-agents")

    async def _choose_allowed_models(self) -> None:
        async def toggle(model_key: str) -> tuple[Choice, ...] | None:
            _toggle_member(self._allowed_model_keys, model_key)
            return self._model_choices()

        await self.open_child(
            ChoiceScreen(
                title="ALLOWED SUBAGENT MODELS",
                placeholder="Filter models…",
                choices=self._model_choices(),
                on_select=toggle,
            )
        )
        self._refresh("agent-policy-models")

    async def _save_field(self, _field_id: str, _value: str) -> tuple[Choice, ...] | None:
        return self._policy_choices()

    def _refresh(self, selected: str) -> None:
        self._replace_choices(self._policy_choices(), selected=selected)

    def _submit(self) -> None:
        try:
            draft = self._draft()
        except ValueError:
            return
        self.dismiss(draft)  # type: ignore[arg-type]

    def _draft(self) -> TuiAgentRuntimeConfiguration:
        if not self._allowed_agent_ids:
            raise ValueError("At least one allowed subagent is required")
        if not self._allowed_model_keys:
            raise ValueError("At least one allowed model is required")
        execution_modes = tuple(
            mode
            for mode, field in (
                ("sync", "agent-policy-sync"),
                ("async", "agent-policy-async"),
            )
            if self._toggles[field]
        )
        if not execution_modes:
            raise ValueError("At least one execution mode is required")
        return TuiAgentRuntimeConfiguration(
            expected_revision=self._revision,
            allowed_agent_id_patterns=tuple(sorted(self._allowed_agent_ids)),
            allowed_model_patterns=tuple(sorted(self._allowed_model_keys)),
            allowed_execution_modes=cast(tuple[ExecutionMode, ...], execution_modes),
            allow_write=self._toggles["agent-policy-write"],
            allow_network=self._toggles["agent-policy-network"],
            allow_mcp=self._toggles["agent-policy-mcp"],
            allow_external_paths=self._toggles["agent-policy-external-paths"],
        )

    def _validation_error(self) -> str:
        try:
            self._draft()
        except ValueError as exc:
            return str(exc)
        return ""

    def _agent_choices(self) -> tuple[Choice, ...]:
        theme = theme_for(self)
        return tuple(
            Choice(
                definition.agent_id,
                definition.name,
                trailing=theme.inline_separator.join(
                    (definition.agent_id, definition.compatibility)
                ),
                group="BUILT-IN" if definition.source == "builtin" else "USER",
                marker=(
                    theme.selected_choice_marker
                    if definition.agent_id in self._allowed_agent_ids
                    else ""
                ),
            )
            for definition in sorted_agent_definitions(self._definitions)
        )

    def _model_choices(self) -> tuple[Choice, ...]:
        theme = theme_for(self)
        return tuple(
            replace(
                choice,
                marker=(
                    theme.selected_choice_marker if choice.value in self._allowed_model_keys else ""
                ),
            )
            for choice in model_choices(self._models, theme=theme)
        )

    def _policy_choices(self) -> tuple[Choice, ...]:
        error = self._validation_error()

        def state(field: str) -> str:
            return _enabled_label(self._toggles[field])

        return (
            Choice(
                "agent-policy-agents",
                "Allowed subagents",
                trailing=f"{len(self._allowed_agent_ids)} of {len(self._definitions)}",
                group="ALLOWED",
            ),
            Choice(
                "agent-policy-models",
                "Allowed models",
                trailing=f"{len(self._allowed_model_keys)} of {len(self._models)}",
                group="ALLOWED",
            ),
            Choice(
                "agent-policy-sync",
                "Wait for results",
                trailing=state("agent-policy-sync"),
                group="EXECUTION",
            ),
            Choice(
                "agent-policy-async",
                "Run in background",
                trailing=state("agent-policy-async"),
                group="EXECUTION",
            ),
            Choice(
                "agent-policy-depth",
                "Nesting limit",
                trailing="1",
                group="EXECUTION",
                disabled=True,
            ),
            Choice(
                "agent-policy-lifecycle",
                "Task duration",
                trailing="Until parent task ends",
                group="EXECUTION",
                disabled=True,
            ),
            Choice(
                "agent-policy-write",
                "Write tools",
                trailing=state("agent-policy-write"),
                group="CAPABILITIES",
            ),
            Choice(
                "agent-policy-network",
                "Network tools",
                trailing=state("agent-policy-network"),
                group="CAPABILITIES",
            ),
            Choice(
                "agent-policy-mcp",
                "MCP tools",
                trailing=state("agent-policy-mcp"),
                group="CAPABILITIES",
            ),
            Choice(
                "agent-policy-external-paths",
                "Files outside workspace",
                trailing=state("agent-policy-external-paths"),
                group="CAPABILITIES",
            ),
            Choice(
                "agent-policy-save", "Save", trailing=error, group="ACTIONS", disabled=bool(error)
            ),
        )


class AgentDefinitionConfigurationScreen(EditableChoiceScreen):
    """Create or edit one user AgentDefinition and its tool selection."""

    _TOOL_PREFIX = "agent-definition-tool:"

    def __init__(
        self,
        tools: tuple[AgentToolOptionView, ...],
        models: tuple[AvailableModel, ...],
        existing: AgentDefinitionDetail | None = None,
        *,
        theme: TuiTheme = DEFAULT_TUI_THEME,
    ) -> None:
        self._initial_tui_theme = theme
        self._existing = existing
        self._tools = {tool.name: tool for tool in tools}
        self._models = models
        configured_model = (
            existing.model
            if existing is not None and existing.model is not None
            else _RUNTIME_MODEL
        )
        self._model_labels = {
            _RUNTIME_MODEL: "Use conversation model",
            **{model.key: model.display_name or model.model_id for model in models},
        }
        if configured_model not in self._model_labels:
            self._model_labels[configured_model] = theme_for(self).inline_separator.join(
                ("Unavailable", configured_model)
            )
        self._values = {
            "agent-definition-id": existing.agent_id if existing is not None else "",
            "agent-definition-name": existing.name if existing is not None else "",
            "agent-definition-description": (existing.description if existing is not None else ""),
            "agent-definition-prompt": existing.system_prompt if existing is not None else "",
            "agent-definition-model": configured_model,
        }
        selection = existing.tool_selection if existing is not None else ToolSelection()
        self._tool_mode = selection.mode
        self._selected_tools = set(selection.names)
        super().__init__(
            title="EDIT SUBAGENT" if existing is not None else "CREATE SUBAGENT",
            placeholder="Filter subagent settings or tools…",
            choices=self._definition_choices(),
            on_save=self._save_field,
        )

    async def _select_choice(self, selected: str) -> None:
        if self._editing_value is None and selected == "agent-definition-tool-mode":
            self._tool_mode = "selected" if self._tool_mode == "all" else "all"
            self._replace_choices(self._definition_choices(), selected=selected)
            return
        if self._editing_value is None and selected == "agent-definition-model":
            model = await self.open_child(
                ChoiceScreen(
                    title="SUBAGENT MODEL",
                    placeholder="Filter models…",
                    choices=self._model_choices(),
                    initial_value=self._values["agent-definition-model"],
                )
            )
            if model is not None:
                self._values["agent-definition-model"] = model
                self._replace_choices(self._definition_choices(), selected=selected)
            return
        if self._editing_value is None and selected.startswith(self._TOOL_PREFIX):
            tool_name = selected.removeprefix(self._TOOL_PREFIX)
            if tool_name in self._selected_tools:
                self._selected_tools.remove(tool_name)
            else:
                self._selected_tools.add(tool_name)
            self._replace_choices(self._definition_choices(), selected=selected)
            return
        if self._editing_value is None and selected == "agent-definition-save":
            self._submit()
            return
        await super()._select_choice(selected)

    async def _save_field(self, field_id: str, value: str) -> tuple[Choice, ...] | None:
        self._values[field_id] = value.strip()
        return self._definition_choices()

    def _submit(self) -> None:
        try:
            draft = self._draft()
        except ValueError:
            return
        self.dismiss(draft)  # type: ignore[arg-type]

    def _draft(self) -> TuiAgentDefinitionConfiguration:
        required = (
            ("agent-definition-id", "Agent ID"),
            ("agent-definition-name", "Name"),
            ("agent-definition-description", "Description"),
        )
        for field, label in required:
            if not self._values[field].strip():
                raise ValueError(f"{label} is required")
        selection = ToolSelection(
            mode=self._tool_mode,
            names=(tuple(sorted(self._selected_tools)) if self._tool_mode == "selected" else ()),
        )
        try:
            definition = AgentDefinition(
                agent_id=self._values["agent-definition-id"],
                name=self._values["agent-definition-name"],
                description=self._values["agent-definition-description"],
                system_prompt=self._values["agent-definition-prompt"],
                tool_selection=selection,
                model=(
                    None
                    if self._values["agent-definition-model"] == _RUNTIME_MODEL
                    else self._values["agent-definition-model"]
                ),
            )
        except ValidationError as exc:
            error = exc.errors(include_url=False)[0]
            field_name = str(error["loc"][-1])
            field = {
                "agent_id": "Agent ID",
                "name": "Name",
                "description": "Description",
                "system_prompt": "Instructions",
            }.get(field_name, field_name.replace("_", " ").title())
            message = str(error["msg"]).removeprefix("Value error, ")
            message = message.removeprefix(f"{field_name} ")
            raise ValueError(f"{field}: {message}") from exc
        return TuiAgentDefinitionConfiguration(
            agent_id=definition.agent_id,
            name=definition.name,
            description=definition.description,
            system_prompt=definition.system_prompt,
            tool_selection=definition.tool_selection,
            model=definition.model,
        )

    def _validation_error(self) -> str:
        try:
            self._draft()
        except ValueError as exc:
            return str(exc)
        return ""

    def _definition_choices(self) -> tuple[Choice, ...]:
        theme = theme_for(self)
        error = self._validation_error()
        choices = [
            Choice(
                "agent-definition-id",
                "Agent ID",
                trailing=self._values["agent-definition-id"] or "Required",
                group="IDENTITY",
                disabled=self._existing is not None,
                edit_value=self._values["agent-definition-id"],
                edit_placeholder="Enter a unique lowercase ID",
            ),
            Choice(
                "agent-definition-name",
                "Name",
                trailing=self._values["agent-definition-name"] or "Required",
                group="IDENTITY",
                edit_value=self._values["agent-definition-name"],
                edit_placeholder="Enter a display name",
            ),
            Choice(
                "agent-definition-description",
                "Description",
                trailing=self._values["agent-definition-description"] or "Required",
                group="INSTRUCTIONS",
                edit_value=self._values["agent-definition-description"],
                edit_placeholder="Describe when this subagent should be used",
            ),
            Choice(
                "agent-definition-prompt",
                "Instructions",
                trailing=f"{len(self._values['agent-definition-prompt'])} characters",
                group="INSTRUCTIONS",
                edit_value=self._values["agent-definition-prompt"],
                edit_placeholder="Enter subagent-specific instructions",
            ),
            Choice(
                "agent-definition-model",
                "Model",
                trailing=self._model_labels[self._values["agent-definition-model"]],
                group="MODEL",
            ),
            Choice(
                "agent-definition-tool-mode",
                "Tool selection",
                trailing="All current tools" if self._tool_mode == "all" else "Selected tools",
                group="TOOLS",
            ),
        ]
        if self._tool_mode == "selected":
            tool_names = sorted(set(self._tools).union(self._selected_tools), key=str.casefold)
            for tool_name in tool_names:
                tool = self._tools.get(tool_name)
                selected = tool_name in self._selected_tools
                choices.append(
                    Choice(
                        f"{self._TOOL_PREFIX}{tool_name}",
                        tool_name,
                        trailing=("Unavailable" if tool is None else tool.effect.replace("_", " ")),
                        group="SELECTED TOOLS",
                        marker=theme.selected_choice_marker if selected else "",
                    )
                )
        choices.append(
            Choice(
                "agent-definition-save",
                "Save",
                trailing=error,
                group="ACTIONS",
                disabled=bool(error),
            )
        )
        return tuple(choices)

    def _model_choices(self) -> tuple[Choice, ...]:
        theme = theme_for(self)
        selected = self._values["agent-definition-model"]
        choices = [
            Choice(
                _RUNTIME_MODEL,
                "Use conversation model",
                marker=(theme.selected_choice_marker if selected == _RUNTIME_MODEL else ""),
                group="SESSION",
            )
        ]
        choices.extend(model_choices(self._models, selected=selected, theme=theme))
        if selected != _RUNTIME_MODEL and not any(model.key == selected for model in self._models):
            choices.append(
                Choice(
                    selected,
                    self._model_labels[selected],
                    marker=theme.selected_choice_marker,
                    group="UNAVAILABLE",
                    disabled=True,
                )
            )
        return tuple(choices)


class CompactionConfigurationScreen(EditableChoiceScreen):
    """Edit compaction policy through the shared grouped inline menu."""

    def __init__(
        self,
        config: CompactionConfigView,
        *,
        models: tuple[AvailableModel, ...],
        strategy_ids: tuple[str, ...],
        theme: TuiTheme = DEFAULT_TUI_THEME,
    ) -> None:
        self._initial_tui_theme = theme
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
            _RUNTIME_MODEL: "Conversation model",
            **{model.key: model.display_name or model.key for model in models},
        }
        if configured_model not in self._model_labels:
            self._model_labels[configured_model] = theme_for(self).inline_separator.join(
                ("Unavailable", configured_model)
            )
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
            placeholder="Search compaction settings…",
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
        theme = theme_for(self)
        selected = self._values["compaction-model"]
        choices = [
            Choice(
                _RUNTIME_MODEL,
                "Conversation model",
                marker=(theme.selected_choice_marker if selected == _RUNTIME_MODEL else ""),
                group="SESSION",
            )
        ]
        choices.extend(model_choices(self._models, selected=selected, theme=theme))
        if selected != _RUNTIME_MODEL and self._model(selected) is None:
            choices.append(
                Choice(
                    selected,
                    self._model_labels[selected],
                    marker=theme.selected_choice_marker,
                    group="UNAVAILABLE",
                    disabled=True,
                )
            )
        return tuple(choices)

    def _submit(self) -> None:
        try:
            draft = self._draft()
        except ValueError:
            return
        self.dismiss(draft)  # type: ignore[arg-type]

    def _draft(self) -> TuiCompactionConfiguration:
        strategy = self._values["compaction-strategy"]
        fallback = self._values["compaction-fallback"]
        model = self._values["compaction-model"]
        trigger = _ratio(self._values["compaction-trigger"], "Start at usage (0–1)")
        target = _ratio(self._values["compaction-target"], "Reduce to usage (0–1)")
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
                "Compaction method",
                trailing=(
                    "Summarize older messages"
                    if self._values["compaction-strategy"] == "builtin.llm_summary"
                    else self._values["compaction-strategy"]
                ),
                group="STRATEGY",
                disabled=True,
            ),
            Choice(
                "compaction-fallback",
                "Backup method",
                trailing=(
                    "None"
                    if fallback == _NO_FALLBACK
                    else "Summarize older messages"
                    if fallback == "builtin.llm_summary"
                    else fallback
                ),
                group="STRATEGY",
                disabled=True,
            ),
            Choice(
                "compaction-auto",
                "Automatic compaction",
                trailing=_enabled_label(self._auto_enabled),
                group="STRATEGY",
            ),
            Choice(
                "compaction-trigger",
                "Start at usage (0–1)",
                trailing=self._values["compaction-trigger"] or "Required",
                group="THRESHOLDS",
                edit_value=self._values["compaction-trigger"],
                edit_placeholder="Enter a ratio from 0 to 1",
            ),
            Choice(
                "compaction-target",
                "Reduce to usage (0–1)",
                trailing=self._values["compaction-target"] or "Required",
                group="THRESHOLDS",
                edit_value=self._values["compaction-target"],
                edit_placeholder="Enter a ratio below the trigger",
            ),
            Choice(
                "compaction-model",
                "Compaction model",
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
                    else THINKING_LABELS[self._values["compaction-reasoning"]]
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
                "Connection type",
                trailing={"stdio": "Local process", "sse": "Remote (SSE)", "http": "Remote (HTTP)"}[
                    self._values["mcp-transport"]
                ],
                group="CONNECTION",
            ),
            Choice(
                "mcp-target",
                "Command / URL",
                trailing=self._values["mcp-target"] or "Required",
                group="CONNECTION",
                edit_value=self._values["mcp-target"],
                edit_placeholder=(
                    "Enter executable only (e.g. npx); put arguments in Arguments (JSON)"
                    if self._values["mcp-transport"] == "stdio"
                    else "Enter the MCP server URL"
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


class PluginConfigurationScreen(EditableChoiceScreen):
    """Edit the application-owned plugin configuration JSON contract."""

    def __init__(
        self,
        plugin: PluginDetail,
        *,
        theme: TuiTheme = DEFAULT_TUI_THEME,
    ) -> None:
        self._initial_tui_theme = theme
        self._plugin_id = plugin.plugin_id
        self._values = {
            "plugin-config-values": json.dumps(
                plugin.configuration_values,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "plugin-config-credentials": json.dumps(
                plugin.credential_refs,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        super().__init__(
            title=theme_for(self).inline_separator.join(("EDIT PLUGIN", plugin.name)),
            placeholder="Filter plugin configuration…",
            choices=self._configuration_choices(),
            on_save=self._save_field,
        )

    async def _select_choice(self, selected: str) -> None:
        if self._editing_value is None and selected == "plugin-config-save":
            self._submit()
            return
        await super()._select_choice(selected)

    async def _save_field(self, field_id: str, value: str) -> tuple[Choice, ...] | None:
        self._values[field_id] = value.strip()
        return self._configuration_choices()

    def _draft(self) -> tuple[dict[str, object], dict[str, str]]:
        values = _json_object(self._values["plugin-config-values"], "Settings (JSON)")
        credentials = _json_string_object(
            self._values["plugin-config-credentials"],
            "Credential references",
        )
        if any(not reference.startswith("env:") for reference in credentials.values()):
            raise ValueError("Credential references must use env: references")
        return values, credentials

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

    def _configuration_choices(self) -> tuple[Choice, ...]:
        error = self._validation_error()
        return (
            Choice(
                "plugin-config-values",
                "Settings (JSON)",
                trailing=self._values["plugin-config-values"],
                group="SETTINGS",
                edit_value=self._values["plugin-config-values"],
                edit_placeholder="Enter a JSON object",
            ),
            Choice(
                "plugin-config-credentials",
                "Credential references",
                trailing=self._values["plugin-config-credentials"],
                group="SETTINGS",
                edit_value=self._values["plugin-config-credentials"],
                edit_placeholder='Enter slot-to-env references, e.g. {"api":"env:API_KEY"}',
            ),
            Choice(
                "plugin-config-save",
                "Save",
                trailing=error,
                group="ACTIONS",
                disabled=bool(error),
            ),
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


def _optional_json_string_list(raw: str, label: str) -> list[str] | None:
    value = _optional_json(raw, label)
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a JSON string array")
    return value


def _json_object(raw: str, label: str) -> dict[str, object]:
    value = _optional_json(raw, label)
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a JSON object")
    return dict(value)


def _json_string_object(raw: str, label: str) -> dict[str, str]:
    value = _json_object(raw, label)
    if any(not isinstance(item, str) for item in value.values()):
        raise ValueError(f"{label} must contain string values")
    return cast(dict[str, str], value)


def _enabled_label(enabled: bool) -> str:
    return "On" if enabled else "Off"


def _toggle_member(values: set[str], value: str) -> None:
    if value in values:
        values.remove(value)
    else:
        values.add(value)


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
