"""Configuration screens for agents."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from pydantic import ValidationError

from ouro.application.contracts.agents import (
    AgentDefinitionDetail,
    AgentDefinitionView,
    AgentToolOptionView,
)
from ouro.application.contracts.configuration import AgentRuntimeConfigView
from ouro.application.contracts.models import AvailableModel
from ouro.core.agents import AgentDefinition, ExecutionMode, ToolSelection
from ouro.core.model_selection import DEFAULT_MODEL
from ouro.tui.features.configuration.values import _RUNTIME_MODEL, _enabled_label, _toggle_member
from ouro.tui.features.providers.model_menus import (
    UNAVAILABLE_MODEL_LABEL,
    model_choices,
    model_selection_label,
)
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.editable import EditableChoiceScreen
from ouro.tui.menus.models import Choice
from ouro.tui.menus.sorting import sorted_agent_definitions
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, theme_for
from ouro.tui.view_models import TuiAgentDefinitionConfiguration, TuiAgentRuntimeConfiguration


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
        system_default_model: AvailableModel | None = None,
    ) -> None:
        self._initial_tui_theme = theme
        self._existing = existing
        self._tools = {tool.name: tool for tool in tools}
        self._models = models
        self._system_default_model = system_default_model
        configured_model = (
            existing.model
            if existing is not None and existing.model is not None
            else _RUNTIME_MODEL
        )
        self._model_labels = {
            _RUNTIME_MODEL: "Use conversation model",
            DEFAULT_MODEL: "Default model",
            **{
                model.key: model_selection_label(model.key, models, theme=theme) for model in models
            },
        }
        if configured_model not in self._model_labels:
            self._model_labels[configured_model] = UNAVAILABLE_MODEL_LABEL
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
        if (
            self._values["agent-definition-model"] == DEFAULT_MODEL
            and self._system_default_model is None
        ):
            raise ValueError("System default endpoint is not configured")
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
            ),
            Choice(
                DEFAULT_MODEL,
                "Default model",
                marker=theme.selected_choice_marker if selected == DEFAULT_MODEL else "",
                group="MODEL SOURCE",
                disabled=self._system_default_model is None,
            ),
        ]
        choices.extend(model_choices(self._models, selected=selected, theme=theme))
        if selected not in {_RUNTIME_MODEL, DEFAULT_MODEL} and not any(
            model.key == selected for model in self._models
        ):
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
