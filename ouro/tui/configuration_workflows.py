from __future__ import annotations

from dataclasses import dataclass, replace
from typing import cast

from textual import work

from ouro.application.agent_models import AgentDefinitionCatalogView, AgentDefinitionView
from ouro.application.configuration_models import (
    MCPServerConfigView,
    SkillPolicyView,
    SkillView,
)
from ouro.config import QueuedInputConsumptionMode
from ouro.core.tools import PermissionMode
from ouro.tui.configuration_screens import (
    AgentDefinitionConfigurationScreen,
    AgentDispatchPolicyScreen,
    CompactionConfigurationScreen,
    MCPConfigurationScreen,
    SkillInstallScreen,
    ToolExecutionConfigurationScreen,
)
from ouro.tui.controller import (
    McpServerStatus,
    TuiAgentDefinitionConfiguration,
    TuiAgentRuntimeConfiguration,
    TuiCompactionConfiguration,
    TuiController,
    TuiMCPServerConfiguration,
    TuiSkillInstallConfiguration,
    TuiSystemStatus,
    TuiToolConfiguration,
    TuiToolExecutionConfiguration,
    project_mcp_system_status,
)
from ouro.tui.menu_labels import PERMISSION_LABELS
from ouro.tui.prompt_workflows import PromptWorkflows
from ouro.tui.screens import Choice, ChoiceScreen, ConfirmationScreen
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, theme_for
from ouro.tui.widgets import ConversationView


@dataclass(frozen=True)
class SystemSettingAction:
    """Stable menu-to-workflow contract; action IDs also back persisted recents."""

    action_id: str
    label: str
    group: str


SYSTEM_SETTING_ACTIONS = (
    SystemSettingAction("tasks.list", "Task list", "Task"),
    SystemSettingAction("tasks.create", "Create task", "Task"),
    SystemSettingAction("tasks.delete", "Delete task", "Task"),
    SystemSettingAction("tasks.refresh", "Update status", "Task"),
    SystemSettingAction("queued_inputs.delivery", "Queued messages", "Settings"),
    SystemSettingAction("compaction.policy", "Context compaction", "Settings"),
    SystemSettingAction("prompts.manage", "Personal prompts", "Settings"),
    SystemSettingAction("data.transfer", "Manage data", "Settings"),
    SystemSettingAction("providers.list", "Manage providers", "Provider"),
    SystemSettingAction("providers.endpoints.list", "Manage endpoints", "Provider"),
    SystemSettingAction(
        "providers.endpoints.use",
        "Set default endpoint",
        "Provider",
    ),
    SystemSettingAction("providers.add", "+ provider", "Provider"),
    SystemSettingAction("providers.endpoints.add", "+ endpoint", "Provider"),
    SystemSettingAction("plugins.list", "Manage plugins", "Plugins"),
    SystemSettingAction("plugins.install", "Install plugin", "Plugins"),
    SystemSettingAction("plugins.remove", "Delete plugin", "Plugins"),
    SystemSettingAction("tools.permissions", "Tool permissions", "Tools"),
    SystemSettingAction("tools.execution", "Tool limits & file access", "Tools"),
    SystemSettingAction("agents.policy", "Subagent permissions", "Subagents"),
    SystemSettingAction("agents.list", "Manage subagents", "Subagents"),
    SystemSettingAction("agents.create", "Create subagent", "Subagents"),
    SystemSettingAction("agents.edit", "Edit subagent", "Subagents"),
    SystemSettingAction("agents.delete", "Delete subagent", "Subagents"),
    SystemSettingAction("mcp.list", "Manage MCP servers", "MCP"),
    SystemSettingAction("mcp.add", "Add MCP server", "MCP"),
    SystemSettingAction("mcp.connect", "Connect MCP server", "MCP"),
    SystemSettingAction("mcp.disconnect", "Disconnect MCP server", "MCP"),
    SystemSettingAction("mcp.edit", "Edit MCP server", "MCP"),
    SystemSettingAction("mcp.remove", "Delete MCP server", "MCP"),
    SystemSettingAction("skills.sources", "Skill sources", "Skills"),
    SystemSettingAction("skills.list", "Manage skills", "Skills"),
    SystemSettingAction("skills.install", "Install skill", "Skills"),
    SystemSettingAction("skills.validate", "Check skill", "Skills"),
)

_QUEUED_INPUT_CONSUMPTION_LABELS: dict[QueuedInputConsumptionMode, str] = {
    "one_by_one": "One at a time",
    "all_at_once": "All at once",
}
_QUEUED_INPUT_CONSUMPTION_DETAILS: dict[QueuedInputConsumptionMode, str] = {
    "one_by_one": "Process each message separately",
    "all_at_once": "Process waiting messages together",
}

_TOOL_PERMISSION_CHOICES: dict[str, PermissionMode | None] = {
    "default": None,
    "auto": "auto",
    "approval": "approval",
    "deny": "deny",
}

_TOOL_SOURCE_GROUPS = {
    "builtin": "Built-in",
    "mcp": "MCP",
    "official_plugin": "Official plugins",
    "third_party_plugin": "Third-party plugins",
    "ai_generated_plugin": "AI-generated plugins",
    "unavailable": "Unavailable",
}
_TOOL_SOURCE_ORDER = {source: index for index, source in enumerate(_TOOL_SOURCE_GROUPS)}


class ConfigurationWorkflowsApp(PromptWorkflows):
    """Own the keyboard-first TUI workflows for durable configuration."""

    _controller: TuiController
    _system_status: TuiSystemStatus

    @work(exclusive=True, group="navigation")
    async def _run_system_setting(
        self,
        action_id: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        if action_id == "tasks.list":
            await self._choose_tasks(menu=menu)
        elif action_id == "tasks.create":
            await self._edit_task(None, menu=menu)
        elif action_id == "tasks.delete":
            await self._choose_tasks(menu=menu, delete=True)
        elif action_id == "tasks.refresh":
            await self._sync_task_status()
            self.notify(
                self._system_status.task_error or "Task status updated",
                severity="error" if self._system_status.task_error else "information",
                markup=False,
            )
        elif action_id == "data.transfer":
            self._choose_data_transfer(menu=menu)
        elif action_id == "prompts.manage":
            await self._configure_prompts(menu=menu)
        elif action_id == "tools.permissions":
            self._choose_tools(menu=menu)
        elif action_id == "tools.execution":
            await self._configure_tool_execution(menu=menu)
        elif action_id == "agents.list":
            await self._choose_agent_definitions(menu=menu)
        elif action_id == "agents.policy":
            await self._configure_agent_policy(menu=menu)
        elif action_id == "agents.create":
            await self._create_agent_definition(menu=menu)
        elif action_id in {"agents.edit", "agents.delete"}:
            await self._choose_agent_definition_action(
                action_id.removeprefix("agents."),
                menu=menu,
            )
        elif action_id == "providers.list":
            await self._choose_providers(menu=menu)
        elif action_id == "providers.add":
            await self._add_provider(menu=menu)
        elif action_id.startswith("providers.endpoints."):
            await self._choose_endpoint_action(
                action_id.removeprefix("providers.endpoints."),
                menu=menu,
            )
        elif action_id.startswith("providers."):
            await self._choose_provider_action(
                action_id.removeprefix("providers."),
                menu=menu,
            )
        elif action_id == "plugins.list":
            await self._choose_plugins(menu=menu)
        elif action_id == "plugins.install":
            await self._install_plugin(menu=menu)
        elif action_id.startswith("plugins."):
            await self._choose_plugin_action(
                action_id.removeprefix("plugins."),
                menu=menu,
            )
        elif action_id == "mcp.list":
            self._choose_mcp(menu=menu)
        elif action_id == "mcp.add":
            await self._edit_mcp(None, menu=menu)
        elif action_id.startswith("mcp."):
            await self._choose_mcp_action(action_id.removeprefix("mcp."), menu=menu)
        elif action_id == "skills.list":
            self._choose_skills(menu=menu)
        elif action_id == "skills.install":
            await self._install_skill(menu=menu)
        elif action_id == "skills.sources":
            await self._configure_skill_sources(menu=menu)
        elif action_id.startswith("skills."):
            await self._choose_skill_action(action_id.removeprefix("skills."), menu=menu)
        elif action_id == "queued_inputs.delivery":
            self._configure_queued_inputs(menu=menu)
        elif action_id == "compaction.policy":
            self._configure_compaction(menu=menu)
        else:
            available = "|".join(action.action_id for action in SYSTEM_SETTING_ACTIONS)
            await self.query_one(ConversationView).add_notice(
                f"Unknown system setting action: {action_id}. Available: {available}",
                error=True,
            )

    @work(exclusive=True, group="configuration")
    async def _configure_queued_inputs(self, *, menu: ChoiceScreen | None = None) -> None:
        try:
            config = await self._controller.queued_input_configuration()
        except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return

        def choices(selected: QueuedInputConsumptionMode) -> tuple[Choice, ...]:
            return tuple(
                Choice(
                    mode,
                    label,
                    trailing=_QUEUED_INPUT_CONSUMPTION_DETAILS[mode],
                    marker=(theme_for(self).selected_choice_marker if mode == selected else ""),
                )
                for mode, label in _QUEUED_INPUT_CONSUMPTION_LABELS.items()
            )

        async def select_mode(value: str) -> tuple[Choice, ...] | None:
            try:
                mode = cast(QueuedInputConsumptionMode, value)
                saved = await self._controller.update_queued_input_consumption(mode)
            except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
                await self.query_one(ConversationView).add_notice(str(exc), error=True)
                return None
            self.notify(
                f"Queued messages: {_QUEUED_INPUT_CONSUMPTION_LABELS[saved.consumption_mode]}"
                " (restart to apply)"
            )
            return choices(saved.consumption_mode)

        if menu is not None:
            menu.push_page(
                title="QUEUED MESSAGES",
                placeholder="Choose how to process waiting messages…",
                choices=choices(config.consumption_mode),
                initial_value=config.consumption_mode,
                on_select=select_mode,
            )
            return
        await self.push_screen_wait(
            ChoiceScreen(
                title="QUEUED MESSAGES",
                placeholder="Choose how to process waiting messages…",
                choices=choices(config.consumption_mode),
                initial_value=config.consumption_mode,
                on_select=select_mode,
            )
        )

    @work(exclusive=True, group="configuration")
    async def _configure_compaction(self, *, menu: ChoiceScreen | None = None) -> None:
        try:
            config = await self._controller.compaction_configuration()
            models = await self._controller.models()
            strategies = self._controller.compaction_strategies()
        except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        screen = CompactionConfigurationScreen(
            config,
            models=models,
            strategy_ids=strategies,
            theme=theme_for(self),
        )
        draft = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if draft is None:
            return
        compaction_draft = cast(TuiCompactionConfiguration, draft)
        try:
            saved = await self._controller.save_compaction_configuration(compaction_draft)
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        route = (
            f"{saved.model.provider_id}/{saved.model.endpoint_id}"
            if saved.model.has_dedicated_endpoint
            else "conversation model"
        )
        self.notify(f"Context compaction saved: {route} (restart to apply)")

    @work(exclusive=True, group="configuration")
    async def _choose_tools(self, *, menu: ChoiceScreen | None = None) -> None:
        try:
            tools = await self._controller.tool_configurations()
        except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        if not tools:
            return
        tool_catalog = {tool.name: tool for tool in tools}
        active_tool: TuiToolConfiguration | None = None

        def show_tool_catalog(*, initial_value: str | None = None) -> None:
            screen.replace_page(
                title="TOOL PERMISSIONS",
                placeholder="Filter tools…",
                choices=_tool_choices(tuple(tool_catalog.values()), theme=theme_for(self)),
                initial_value=initial_value,
                on_select=select_tool_or_permission,
                on_cancel=cancel_submenu,
            )

        async def select_tool_or_permission(selected: str) -> tuple[Choice, ...] | None:
            nonlocal active_tool
            if active_tool is None:
                tool = tool_catalog.get(selected)
                if tool is None:
                    await self.query_one(ConversationView).add_notice(
                        f"Tool not found: {selected}",
                        error=True,
                    )
                    return None
                if tool.permission_control == "tool":
                    self.notify(
                        f"{tool.name} permissions are controlled by its plugin",
                        severity="warning",
                    )
                    return None
                active_tool = tool
                current = tool.permission_override or "default"
                screen.replace_page(
                    title=f"TOOL PERMISSION{theme_for(self).inline_separator}{tool.name}",
                    placeholder="Choose a permission…",
                    choices=_tool_permission_choices(
                        current,
                        default_permission=tool.inherited_permission,
                        theme=theme_for(self),
                    ),
                    initial_value=current,
                    on_select=select_tool_or_permission,
                    on_cancel=cancel_submenu,
                    interaction_hint=ChoiceScreen.BACK_HINT,
                )
                return None

            try:
                await self._controller.set_tool_permission(
                    active_tool.name,
                    _TOOL_PERMISSION_CHOICES[selected],
                )
                refreshed = await self._controller.tool_configurations()
            except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
                await self.query_one(ConversationView).add_notice(str(exc), error=True)
                return None
            selected_catalog_value = active_tool.name
            tool_catalog.clear()
            tool_catalog.update({tool.name: tool for tool in refreshed})
            active_tool = None
            show_tool_catalog(initial_value=selected_catalog_value)
            return None

        def cancel_submenu() -> bool:
            nonlocal active_tool
            if active_tool is None:
                return False
            initial_value = active_tool.name
            active_tool = None
            show_tool_catalog(initial_value=initial_value)
            return True

        if menu is not None:
            screen = menu
            screen.push_page(
                title="TOOL PERMISSIONS",
                placeholder="Filter tools…",
                choices=_tool_choices(tools, theme=theme_for(self)),
                on_select=select_tool_or_permission,
                on_cancel=cancel_submenu,
            )
            return
        screen = ChoiceScreen(
            title="TOOL PERMISSIONS",
            placeholder="Filter tools…",
            choices=_tool_choices(tools, theme=theme_for(self)),
            on_select=select_tool_or_permission,
            on_cancel=cancel_submenu,
        )
        await self.push_screen_wait(screen)

    async def _configure_tool_execution(self, *, menu: ChoiceScreen | None = None) -> None:
        try:
            current = await self._controller.tool_execution_configuration()
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        screen = ToolExecutionConfigurationScreen(current)
        draft = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if draft is None:
            return
        tool_execution_draft = cast(TuiToolExecutionConfiguration, draft)
        try:
            updated = await self._controller.update_tool_execution(tool_execution_draft)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        scope = "workspace only" if updated.workspace_only else "external paths allowed"
        self.notify(f"Tool execution saved: {updated.mode}, {scope} (next restart)")

    async def _configure_agent_policy(self, *, menu: ChoiceScreen | None = None) -> None:
        try:
            current = await self._controller.agent_runtime_configuration()
            catalog = await self._controller.agent_definition_catalog()
            models = await self._controller.models()
        except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        screen = AgentDispatchPolicyScreen(
            current,
            definitions=catalog.definitions,
            models=models,
            theme=theme_for(self),
        )
        draft = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if draft is None:
            return
        try:
            await self._controller.update_agent_runtime(cast(TuiAgentRuntimeConfiguration, draft))
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return

    async def _choose_agent_definitions(
        self,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        try:
            catalog = await self._controller.agent_definition_catalog()
        except Exception as exc:  # noqa: BLE001 - catalog errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        screen: ChoiceScreen

        async def refresh_definitions() -> tuple[Choice, ...] | None:
            try:
                refreshed = await self._controller.agent_definition_catalog()
            except Exception as exc:  # noqa: BLE001
                await self.query_one(ConversationView).add_notice(str(exc), error=True)
                return None
            return _agent_definition_choices(refreshed.definitions, theme=theme_for(self))

        async def select_definition(agent_id: str) -> tuple[Choice, ...] | None:
            try:
                current = await self._controller.agent_definition_catalog()
            except Exception as exc:  # noqa: BLE001 - catalog errors are user-visible.
                await self.query_one(ConversationView).add_notice(str(exc), error=True)
                return None
            definition = next(
                (item for item in current.definitions if item.agent_id == agent_id),
                None,
            )
            if definition is None or not definition.editable:
                return None
            await self._manage_agent_definition(
                definition,
                expected_revision=current.revision,
                menu=screen,
            )
            return None

        if menu is not None:
            screen = menu
            screen.push_page(
                title="SUBAGENTS",
                placeholder="Filter subagents…",
                choices=_agent_definition_choices(catalog.definitions, theme=theme_for(self)),
                on_select=select_definition,
                on_refresh=refresh_definitions,
            )
            return
        screen = ChoiceScreen(
            title="SUBAGENTS",
            placeholder="Filter subagents…",
            choices=_agent_definition_choices(catalog.definitions, theme=theme_for(self)),
            on_select=select_definition,
            on_refresh=refresh_definitions,
        )
        await self.push_screen_wait(screen)

    async def _manage_agent_definition(
        self,
        definition: AgentDefinitionView,
        *,
        expected_revision: int,
        menu: ChoiceScreen,
    ) -> None:
        async def select_action(action: str) -> tuple[Choice, ...] | None:
            if action == "edit":
                saved = await self._edit_agent_definition(
                    definition.agent_id,
                    expected_revision=expected_revision,
                    menu=menu,
                )
            else:
                saved = await self._delete_agent_definition(
                    definition.agent_id,
                    expected_revision=expected_revision,
                    menu=menu,
                )
            if saved is not None:
                menu.pop_page(
                    choices=_agent_definition_choices(saved.definitions, theme=theme_for(self)),
                    initial_value=definition.agent_id,
                )
            return None

        menu.push_page(
            title=f"SUBAGENT{theme_for(self).inline_separator}{definition.name}",
            placeholder="Choose an action…",
            choices=(
                Choice("edit", "Edit subagent"),
                Choice("delete", "Delete subagent"),
            ),
            on_select=select_action,
        )

    async def _create_agent_definition(self, *, menu: ChoiceScreen | None = None) -> None:
        try:
            catalog = await self._controller.agent_definition_catalog()
            tools = await self._controller.agent_definition_tools()
            models = await self._controller.models()
        except Exception as exc:  # noqa: BLE001 - catalog errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        screen = AgentDefinitionConfigurationScreen(tools, models, theme=theme_for(self))
        draft = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if draft is None:
            return
        definition = cast(TuiAgentDefinitionConfiguration, draft)
        try:
            await self._controller.create_agent_definition(
                definition,
                expected_revision=catalog.revision,
            )
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return

    async def _choose_agent_definition_action(
        self,
        action: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        try:
            catalog = await self._controller.agent_definition_catalog()
        except Exception as exc:  # noqa: BLE001 - catalog errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        choices = _agent_definition_choices(catalog.definitions, theme=theme_for(self))

        async def select_agent(agent_id: str) -> tuple[Choice, ...] | None:
            nonlocal catalog
            if action == "edit":
                saved = await self._edit_agent_definition(
                    agent_id,
                    expected_revision=catalog.revision,
                    menu=menu,
                )
            else:
                saved = await self._delete_agent_definition(
                    agent_id,
                    expected_revision=catalog.revision,
                    menu=menu,
                )
            if saved is None:
                return None
            catalog = saved
            if menu is not None:
                menu.pop_page()
                return None
            return _agent_definition_choices(saved.definitions, theme=theme_for(self))

        title = "EDIT SUBAGENT" if action == "edit" else "DELETE SUBAGENT"
        if menu is not None:
            menu.push_page(
                title=title,
                placeholder="Filter subagents…",
                choices=choices,
                on_select=select_agent,
            )
            return
        await self.push_screen_wait(
            ChoiceScreen(
                title=title,
                placeholder="Filter subagents…",
                choices=choices,
                on_select=select_agent,
            )
        )

    async def _edit_agent_definition(
        self,
        agent_id: str,
        *,
        expected_revision: int,
        menu: ChoiceScreen | None = None,
    ) -> AgentDefinitionCatalogView | None:
        try:
            existing = await self._controller.agent_definition(agent_id)
            tools = await self._controller.agent_definition_tools()
            models = await self._controller.models()
        except Exception as exc:  # noqa: BLE001 - catalog errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return None
        screen = AgentDefinitionConfigurationScreen(
            tools,
            models,
            existing,
            theme=theme_for(self),
        )
        draft = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if draft is None:
            return None
        definition = cast(TuiAgentDefinitionConfiguration, draft)
        try:
            saved = await self._controller.update_agent_definition(
                definition,
                expected_revision=expected_revision,
            )
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return None
        return saved

    async def _delete_agent_definition(
        self,
        agent_id: str,
        *,
        expected_revision: int,
        menu: ChoiceScreen | None = None,
    ) -> AgentDefinitionCatalogView | None:
        screen = ConfirmationScreen(
            title="DELETE SUBAGENT",
            message=(
                f"Delete {agent_id}? Current tasks can finish; new tasks cannot use this subagent."
            ),
            confirm_label="Delete",
        )
        selected = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if not ConfirmationScreen.confirmed(selected):
            return None
        try:
            saved = await self._controller.delete_agent_definition(
                agent_id,
                expected_revision=expected_revision,
            )
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return None
        return saved

    @work(exclusive=True, group="configuration")
    async def _choose_mcp(
        self,
        selector: str | None = None,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        try:
            configs = await self._controller.mcp_configurations()
            runtime = {item.server_id: item for item in self._controller.mcp_servers()}
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        selected = selector
        if selected is None:
            choices = _mcp_choices(configs, runtime, theme=theme_for(self))
            if not choices:
                return
            if menu is not None:
                servers = {config.server_id: config for config in configs}

                async def refresh_servers() -> tuple[Choice, ...] | None:
                    try:
                        refreshed = await self._controller.mcp_configurations()
                        refreshed_runtime = {
                            item.server_id: item for item in self._controller.mcp_servers()
                        }
                    except Exception as exc:  # noqa: BLE001
                        await self.query_one(ConversationView).add_notice(str(exc), error=True)
                        return None
                    servers.clear()
                    servers.update({config.server_id: config for config in refreshed})
                    return _mcp_choices(refreshed, refreshed_runtime, theme=theme_for(self))

                async def select_server(server_id: str) -> tuple[Choice, ...] | None:
                    if server_id not in servers:
                        return None
                    await self._manage_mcp(server_id, menu=menu)
                    return None

                menu.push_page(
                    title="MCP SERVERS",
                    placeholder="Filter servers…",
                    choices=choices,
                    on_select=select_server,
                    on_refresh=refresh_servers,
                )
                return
            selected = await self.push_screen_wait(
                ChoiceScreen(title="MCP SERVERS", placeholder="Filter servers…", choices=choices)
            )
        if selected is None:
            return
        if not any(item.server_id == selected for item in configs):
            await self.query_one(ConversationView).add_notice(
                f"MCP server not found: {selected}",
                error=True,
            )
            return
        await self._manage_mcp(selected)

    async def _choose_mcp_action(
        self,
        action: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        if action not in {"connect", "disconnect", "toggle", "edit", "remove"}:
            await self.query_one(ConversationView).add_notice(
                f"Unknown MCP action: {action}",
                error=True,
            )
            return
        try:
            configs = await self._controller.mcp_configurations()
            runtime = {item.server_id: item for item in self._controller.mcp_servers()}
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return

        action_label = {
            "connect": "CONNECT",
            "disconnect": "DISCONNECT",
            "toggle": "ENABLE OR DISABLE",
            "edit": "EDIT",
            "remove": "DELETE",
        }[action]
        if not configs:
            return
        choices = _mcp_action_choices(action, configs, runtime, theme=theme_for(self))
        if action == "toggle":
            servers = {config.server_id: config for config in configs}

            async def toggle_mcp(server_id: str) -> tuple[Choice, ...] | None:
                config = servers[server_id]
                if not await self._set_mcp_enabled(
                    server_id,
                    enabled=not config.enabled,
                ):
                    return None
                try:
                    refreshed = await self._controller.mcp_configurations()
                    refreshed_runtime = {
                        item.server_id: item for item in self._controller.mcp_servers()
                    }
                except Exception as exc:  # noqa: BLE001
                    await self.query_one(ConversationView).add_notice(str(exc), error=True)
                    return None
                servers.clear()
                servers.update({config.server_id: config for config in refreshed})
                return tuple(
                    Choice(
                        config.server_id,
                        config.name,
                        group=config.transport.upper(),
                        trailing=_mcp_labels(
                            config,
                            refreshed_runtime.get(config.server_id),
                            theme=theme_for(self),
                        ),
                    )
                    for config in sorted(
                        servers.values(),
                        key=lambda item: (item.transport, item.name.casefold()),
                    )
                )

            if menu is not None:
                menu.push_page(
                    title="MCP SERVER AVAILABILITY",
                    placeholder="Filter servers…",
                    choices=choices,
                    on_select=toggle_mcp,
                )
            else:
                await self.push_screen_wait(
                    ChoiceScreen(
                        title="MCP SERVER AVAILABILITY",
                        placeholder="Filter servers…",
                        choices=choices,
                        on_select=toggle_mcp,
                    )
                )
            return
        if menu is not None:
            servers = {config.server_id: config for config in configs}
            loading_servers: set[str] = set()

            def current_action_choices(
                current_runtime: dict[str, McpServerStatus] | None = None,
            ) -> tuple[Choice, ...]:
                return _mcp_action_choices(
                    action,
                    tuple(servers.values()),
                    runtime if current_runtime is None else current_runtime,
                    loading=loading_servers,
                    theme=theme_for(self),
                )

            async def refresh_action_candidates() -> tuple[Choice, ...] | None:
                try:
                    refreshed_configs = await self._controller.mcp_configurations()
                    refreshed_runtime = {
                        item.server_id: item for item in self._controller.mcp_servers()
                    }
                except Exception as exc:  # noqa: BLE001
                    await self.query_one(ConversationView).add_notice(str(exc), error=True)
                    return None
                servers.clear()
                servers.update({config.server_id: config for config in refreshed_configs})
                return current_action_choices(refreshed_runtime)

            async def finish_runtime_action(
                server_id: str,
                operation: str,
                page_revision: int,
            ) -> None:
                try:
                    await self._run_mcp_action(server_id, operation)
                finally:
                    loading_servers.discard(server_id)
                try:
                    refreshed_configs = await self._controller.mcp_configurations()
                    refreshed_runtime = {
                        item.server_id: item for item in self._controller.mcp_servers()
                    }
                except Exception as exc:  # noqa: BLE001
                    await self.query_one(ConversationView).add_notice(str(exc), error=True)
                    return
                refreshed_by_id = {config.server_id: config for config in refreshed_configs}
                for current_id in tuple(servers):
                    config = refreshed_by_id.get(current_id)
                    if config is None:
                        servers.pop(current_id, None)
                    else:
                        servers[current_id] = config
                if not menu.update_choices(
                    current_action_choices(refreshed_runtime),
                    expected_revision=page_revision,
                    preferred_value=server_id,
                ):
                    await menu.refresh_choices()

            async def select_server(server_id: str) -> tuple[Choice, ...] | None:
                config = servers.get(server_id)
                if config is None:
                    return await refresh_action_candidates()
                if action == "edit":
                    await self._edit_mcp(config, menu=menu)
                elif action == "remove":
                    await self._remove_mcp(server_id, menu=menu)
                else:
                    if server_id in loading_servers:
                        return current_action_choices()
                    loading_servers.add(server_id)
                    page_revision = menu.page_revision
                    self.run_worker(
                        finish_runtime_action(
                            server_id,
                            "start" if action == "connect" else "stop",
                            page_revision,
                        ),
                        group=f"mcp-menu-{server_id}",
                    )
                    return current_action_choices()
                return None

            menu.push_page(
                title=f"{action_label} MCP SERVER",
                placeholder="Filter servers…",
                choices=choices,
                on_select=select_server,
                on_refresh=refresh_action_candidates,
            )
            return
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=f"{action_label} MCP SERVER",
                placeholder="Filter servers…",
                choices=choices,
            )
        )
        if selected is None:
            return
        config = next(item for item in configs if item.server_id == selected)
        if action == "edit":
            await self._edit_mcp(config)
        elif action == "remove":
            await self._remove_mcp(selected)
        else:
            await self._run_mcp_action(
                selected,
                "start" if action == "connect" else "stop",
            )

    async def _manage_mcp(
        self,
        server_id: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        try:
            config = await self._controller.mcp_configuration(server_id)
            status = next(
                (item for item in self._controller.mcp_servers() if item.server_id == server_id),
                None,
            )
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return

        actions = _mcp_management_choices(config, status)

        if menu is not None:
            loading_action: str | None = None

            async def refresh_mcp_actions() -> tuple[Choice, ...] | None:
                nonlocal config, status
                try:
                    config = await self._controller.mcp_configuration(server_id)
                    status = next(
                        (
                            item
                            for item in self._controller.mcp_servers()
                            if item.server_id == server_id
                        ),
                        None,
                    )
                except Exception as exc:  # noqa: BLE001
                    await self.query_one(ConversationView).add_notice(str(exc), error=True)
                    return None
                return _mcp_management_choices(
                    config,
                    status,
                    loading_action=loading_action,
                )

            async def finish_runtime_action(action: str, page_revision: int) -> None:
                nonlocal config, status, loading_action
                try:
                    await self._run_mcp_action(server_id, action)
                finally:
                    loading_action = None
                try:
                    configs = await self._controller.mcp_configurations()
                    runtime = {item.server_id: item for item in self._controller.mcp_servers()}
                    config = next(item for item in configs if item.server_id == server_id)
                    status = runtime.get(server_id)
                except Exception as exc:  # noqa: BLE001
                    await self.query_one(ConversationView).add_notice(str(exc), error=True)
                    return
                if menu.update_choices(
                    _mcp_management_choices(config, status),
                    expected_revision=page_revision,
                    preferred_value=action,
                ):
                    menu.update_parent_choices(
                        _mcp_choices(configs, runtime, theme=theme_for(self)),
                        initial_value=server_id,
                    )
                else:
                    await menu.refresh_choices()

            async def select_action(selected: str) -> tuple[Choice, ...] | None:
                nonlocal config, status, loading_action
                if selected == "edit":
                    await self._edit_mcp(config, menu=menu)
                elif selected == "toggle":
                    if not await self._set_mcp_enabled(
                        server_id,
                        enabled=not config.enabled,
                    ):
                        return None
                    configs = await self._controller.mcp_configurations()
                    runtime = {item.server_id: item for item in self._controller.mcp_servers()}
                    config = next(item for item in configs if item.server_id == server_id)
                    status = runtime.get(server_id)
                    menu.update_parent_choices(
                        _mcp_choices(configs, runtime, theme=theme_for(self)),
                        initial_value=server_id,
                    )
                    return _mcp_management_choices(config, status)
                elif selected == "check":
                    await self._check_mcp(server_id)
                elif selected in {"start", "stop", "restart", "refresh"}:
                    if loading_action is not None:
                        return _mcp_management_choices(
                            config,
                            status,
                            loading_action=loading_action,
                        )
                    loading_action = selected
                    self.run_worker(
                        finish_runtime_action(selected, menu.page_revision),
                        group=f"mcp-menu-{server_id}",
                    )
                    return _mcp_management_choices(
                        config,
                        status,
                        loading_action=loading_action,
                    )
                elif selected == "remove":
                    if await self._remove_mcp(server_id, menu=menu):
                        await menu.pop_page_refreshed()
                return None

            menu.push_page(
                title=f"MCP{theme_for(self).inline_separator}{config.name}",
                placeholder="Choose an action…",
                choices=actions,
                on_select=select_action,
                on_refresh=refresh_mcp_actions,
            )
            return
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=f"MCP{theme_for(self).inline_separator}{config.name}",
                placeholder="Choose an action…",
                choices=actions,
            )
        )
        if selected == "edit":
            await self._edit_mcp(config)
        elif selected == "toggle":
            await self._set_mcp_enabled(server_id, enabled=not config.enabled)
        elif selected == "check":
            await self._check_mcp(server_id)
        elif selected in {"start", "stop", "restart", "refresh"}:
            await self._run_mcp_action(server_id, selected)
        elif selected == "remove":
            await self._remove_mcp(server_id)

    async def _edit_mcp(
        self,
        existing: MCPServerConfigView | None,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        screen = MCPConfigurationScreen(existing, theme=theme_for(self))
        draft = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if draft is None:
            return
        mcp_draft = cast(TuiMCPServerConfiguration, draft)
        try:
            saved = await self._controller.save_mcp_configuration(mcp_draft)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self._refresh_mcp_system_status()
        self.notify(
            f"MCP configuration saved: {saved.server_id}{theme_for(self).inline_separator}"
            "tool policy active; restart for connection changes"
        )

    async def _check_mcp(self, server_id: str) -> None:
        try:
            result = await self._controller.check_mcp_server(server_id)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        if result.ok:
            self.notify(
                f"MCP check passed: {server_id}{theme_for(self).inline_separator}"
                f"{len(result.tool_names)} tool(s)"
            )
        else:
            await self.query_one(ConversationView).add_notice(
                f"MCP check failed: {server_id}{theme_for(self).inline_separator}"
                f"{result.error or result.status}",
                error=True,
            )

    async def _run_mcp_action(
        self,
        server_id: str,
        action: str,
    ) -> McpServerStatus | None:
        operations = {
            "start": self._controller.start_mcp_server,
            "stop": self._controller.stop_mcp_server,
            "restart": self._controller.restart_mcp_server,
            "refresh": self._controller.refresh_mcp_tools,
        }
        try:
            status = await operations[action](server_id)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return None
        self._refresh_mcp_system_status()
        if status.status == "failed":
            self.notify(
                f"MCP {server_id}: {status.status}{theme_for(self).inline_separator}"
                f"{status.tool_count} tool(s)",
                severity="error",
            )
        return status

    async def _remove_mcp(
        self,
        server_id: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> bool:
        screen = ConfirmationScreen(
            title="DELETE MCP SERVER",
            message=(f"Delete {server_id}? Takes effect after restarting the app."),
            confirm_label="Delete",
        )
        selected = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if not ConfirmationScreen.confirmed(selected):
            return False
        try:
            await self._controller.remove_mcp_configuration(server_id)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return False
        self._refresh_mcp_system_status()
        self.notify(f"MCP server deleted: {server_id} (next restart)")
        return True

    async def _set_mcp_enabled(self, server_id: str, *, enabled: bool) -> bool:
        try:
            await self._controller.set_mcp_enabled(server_id, enabled=enabled)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return False
        self._refresh_mcp_system_status()
        return True

    async def _configure_skill_sources(
        self,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        try:
            policy = await self._controller.skill_policy()
        except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return

        async def refresh_sources() -> tuple[Choice, ...] | None:
            nonlocal policy
            try:
                policy = await self._controller.skill_policy()
            except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
                await self.query_one(ConversationView).add_notice(str(exc), error=True)
                return None
            return _skill_source_choices(policy, theme=theme_for(self))

        async def toggle_source(source_id: str) -> tuple[Choice, ...] | None:
            nonlocal policy
            try:
                if source_id == "discovery":
                    policy = await self._controller.update_skill_discovery(
                        enabled=not policy.enabled
                    )
                else:
                    source = next(
                        (item for item in policy.sources if item.source_id == source_id),
                        None,
                    )
                    if source is None:
                        return None
                    policy = await self._controller.update_skill_discovery(
                        source_updates={source_id: not source.enabled}
                    )
            except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
                await self.query_one(ConversationView).add_notice(str(exc), error=True)
                return None
            self._refresh_skill_system_status()
            return _skill_source_choices(policy, theme=theme_for(self))

        choices = _skill_source_choices(policy, theme=theme_for(self))
        if menu is not None:
            menu.push_page(
                title="SKILL SOURCES",
                placeholder="Filter skill sources…",
                choices=choices,
                on_select=toggle_source,
                on_refresh=refresh_sources,
            )
            return
        await self.push_screen_wait(
            ChoiceScreen(
                title="SKILL SOURCES",
                placeholder="Filter skill sources…",
                choices=choices,
                on_select=toggle_source,
                on_refresh=refresh_sources,
            )
        )

    @work(exclusive=True, group="configuration")
    async def _choose_skills(
        self,
        selector: str | None = None,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        try:
            catalog = await self._controller.skill_catalog()
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        selected = selector
        if selected is None:
            choices = tuple(_skill_choice(skill, theme=theme_for(self)) for skill in catalog.skills)
            if not choices:
                return
            if menu is not None:
                skills = {skill.name: skill for skill in catalog.skills}

                async def refresh_skills() -> tuple[Choice, ...] | None:
                    try:
                        refreshed = await self._controller.skill_catalog()
                    except Exception as exc:  # noqa: BLE001
                        await self.query_one(ConversationView).add_notice(str(exc), error=True)
                        return None
                    skills.clear()
                    skills.update({skill.name: skill for skill in refreshed.skills})
                    return tuple(
                        _skill_choice(skill, theme=theme_for(self)) for skill in refreshed.skills
                    )

                async def select_skill(name: str) -> tuple[Choice, ...] | None:
                    if name not in skills:
                        return None
                    await self._manage_skill(name, menu=menu)
                    return None

                menu.push_page(
                    title="SKILLS",
                    placeholder="Filter skills…",
                    choices=choices,
                    on_select=select_skill,
                    on_refresh=refresh_skills,
                )
                return
            selected = await self.push_screen_wait(
                ChoiceScreen(title="SKILLS", placeholder="Filter skills…", choices=choices)
            )
        if selected is None:
            return
        if not any(item.name == selected for item in catalog.skills):
            await self.query_one(ConversationView).add_notice(
                f"Skill not found: {selected}",
                error=True,
            )
            return
        await self._manage_skill(selected)

    async def _choose_skill_action(
        self,
        action: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        if action not in {"toggle", "validate", "update", "remove"}:
            await self.query_one(ConversationView).add_notice(
                f"Unknown skill action: {action}",
                error=True,
            )
            return
        try:
            catalog = await self._controller.skill_catalog()
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        candidates = (
            tuple(skill for skill in catalog.skills if skill.managed)
            if action in {"update", "remove"}
            else catalog.skills
        )
        if not candidates:
            return
        action_label = {
            "toggle": "ENABLE OR DISABLE",
            "validate": "CHECK",
            "update": "UPDATE",
            "remove": "DELETE",
        }[action]
        choices = tuple(_skill_choice(skill, theme=theme_for(self)) for skill in candidates)
        if action == "toggle":
            skills = {skill.name: skill for skill in candidates}

            async def toggle_skill(name: str) -> tuple[Choice, ...] | None:
                skill = skills[name]
                if not await self._set_skill_enabled(name, enabled=not skill.enabled):
                    return None
                try:
                    refreshed = await self._controller.skill_catalog()
                except Exception as exc:  # noqa: BLE001
                    await self.query_one(ConversationView).add_notice(
                        str(exc),
                        error=True,
                    )
                    return None
                skills.clear()
                skills.update({skill.name: skill for skill in refreshed.skills})
                return tuple(
                    _skill_choice(skill, theme=theme_for(self)) for skill in skills.values()
                )

            if menu is not None:
                menu.push_page(
                    title=f"{action_label} SKILL",
                    placeholder="Filter skills…",
                    choices=choices,
                    on_select=toggle_skill,
                )
            else:
                await self.push_screen_wait(
                    ChoiceScreen(
                        title=f"{action_label} SKILL",
                        placeholder="Filter skills…",
                        choices=choices,
                        on_select=toggle_skill,
                    )
                )
            return
        if menu is not None:
            skills = {skill.name: skill for skill in candidates}

            async def refresh_skill_candidates() -> tuple[Choice, ...] | None:
                try:
                    refreshed = await self._controller.skill_catalog()
                except Exception as exc:  # noqa: BLE001
                    await self.query_one(ConversationView).add_notice(str(exc), error=True)
                    return None
                refreshed_candidates = (
                    tuple(skill for skill in refreshed.skills if skill.managed)
                    if action in {"update", "remove"}
                    else refreshed.skills
                )
                skills.clear()
                skills.update({skill.name: skill for skill in refreshed_candidates})
                return tuple(
                    _skill_choice(skill, theme=theme_for(self)) for skill in refreshed_candidates
                )

            async def select_skill(name: str) -> tuple[Choice, ...] | None:
                skill = skills[name]
                if action == "validate":
                    await self._validate_skill(skill.name)
                elif action == "update":
                    await self._update_skill(skill.name)
                else:
                    await self._remove_skill(skill.name, menu=menu)
                return None

            menu.push_page(
                title=f"{action_label} SKILL",
                placeholder="Filter skills…",
                choices=choices,
                on_select=select_skill,
                on_refresh=refresh_skill_candidates,
            )
            return
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=f"{action_label} SKILL",
                placeholder="Filter skills…",
                choices=choices,
            )
        )
        if selected is None:
            return
        skill = next(item for item in candidates if item.name == selected)
        if action == "validate":
            await self._validate_skill(skill.name)
        elif action == "update":
            await self._update_skill(skill.name)
        else:
            await self._remove_skill(skill.name)

    async def _manage_skill(
        self,
        name: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        try:
            catalog = await self._controller.skill_catalog()
            skill = next(
                (item for item in catalog.skills if item.name == name),
                None,
            )
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        if skill is None:
            await self.query_one(ConversationView).add_notice(
                f"Skill not found: {name}",
                error=True,
            )
            return
        plugin_id = _plugin_skill_owner(skill)
        if plugin_id is not None:
            self.notify(f"Skill {name} is managed by plugin {plugin_id}")
            return

        actions = _skill_management_choices(skill)
        if menu is not None:

            async def refresh_skill_actions() -> tuple[Choice, ...] | None:
                nonlocal skill
                try:
                    catalog = await self._controller.skill_catalog()
                except Exception as exc:  # noqa: BLE001
                    await self.query_one(ConversationView).add_notice(str(exc), error=True)
                    return None
                refreshed = next(
                    (item for item in catalog.skills if item.name == name),
                    None,
                )
                if refreshed is None:
                    return ()
                skill = refreshed
                return _skill_management_choices(skill)

            async def select_action(selected: str) -> tuple[Choice, ...] | None:
                nonlocal skill
                if selected in {"enable", "disable"}:
                    if not await self._set_skill_enabled(
                        name,
                        enabled=selected == "enable",
                    ):
                        return None
                    catalog = await self._controller.skill_catalog()
                    refreshed = next(
                        (item for item in catalog.skills if item.name == name),
                        None,
                    )
                    if refreshed is None:
                        await self.query_one(ConversationView).add_notice(
                            f"Skill not found after state change: {name}",
                            error=True,
                        )
                        return None
                    skill = refreshed
                    menu.update_parent_choices(
                        tuple(
                            _skill_choice(item, theme=theme_for(self)) for item in catalog.skills
                        ),
                        initial_value=name,
                    )
                    return _skill_management_choices(skill)
                elif selected == "validate":
                    await self._validate_skill(name)
                elif selected == "update":
                    await self._update_skill(name)
                elif selected == "remove":
                    await self._remove_skill(name, menu=menu)
                return None

            menu.push_page(
                title=f"SKILL{theme_for(self).inline_separator}{name}",
                placeholder="Choose an action…",
                choices=actions,
                on_select=select_action,
                on_refresh=refresh_skill_actions,
            )
            return
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=f"SKILL{theme_for(self).inline_separator}{name}",
                placeholder="Choose an action…",
                choices=actions,
            )
        )
        if selected in {"enable", "disable"}:
            await self._set_skill_enabled(name, enabled=selected == "enable")
        elif selected == "validate":
            await self._validate_skill(name)
        elif selected == "update":
            await self._update_skill(name)
        elif selected == "remove":
            await self._remove_skill(name)

    async def _install_skill(self, *, menu: ChoiceScreen | None = None) -> None:
        screen = SkillInstallScreen()
        draft = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if draft is None:
            return
        skill_draft = cast(TuiSkillInstallConfiguration, draft)
        try:
            result = await self._controller.install_skills(skill_draft)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        names = ", ".join(item.name for item in result.skills) or "none"
        self.notify(f"Skills installed: {names} (next restart)")

    async def _set_skill_enabled(self, name: str, *, enabled: bool) -> bool:
        try:
            await self._controller.set_skill_enabled(name, enabled=enabled)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return False
        self._refresh_skill_system_status()
        return True

    def _refresh_skill_system_status(self) -> None:
        skill_names = self._controller.runtime_skill_names()
        self._system_status = replace(
            self._system_status,
            skill_count=len(skill_names),
            skill_names=skill_names,
        )
        self.query_one(ConversationView).set_system_status(self._system_status)

    async def _validate_skill(self, name: str) -> None:
        try:
            result = await self._controller.validate_skill(name)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        if result.valid:
            self.notify(f"Skill check passed: {name}")
            return
        messages = "; ".join(item.message for item in result.diagnostics) or "invalid skill"
        await self.query_one(ConversationView).add_notice(
            f"Skill check failed: {name}{theme_for(self).inline_separator}{messages}",
            error=True,
        )

    async def _update_skill(self, name: str) -> None:
        try:
            await self._controller.update_skill(name)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self.notify(f"Skill updated: {name} (next restart)")

    async def _remove_skill(
        self,
        name: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        screen = ConfirmationScreen(
            title="DELETE SKILL",
            message=f"Delete {name}? Takes effect after restarting the app.",
            confirm_label="Delete",
        )
        selected = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if not ConfirmationScreen.confirmed(selected):
            return
        try:
            await self._controller.remove_skill(name)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self.notify(f"Skill deleted: {name} (next restart)")

    def _refresh_mcp_system_status(self) -> None:
        runtime = self._controller.mcp_servers()
        self._system_status = project_mcp_system_status(self._system_status, runtime)
        self.query_one(ConversationView).set_system_status(self._system_status)


def _tool_choices(
    tools: tuple[TuiToolConfiguration, ...],
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[Choice, ...]:
    ordered = sorted(
        tools,
        key=lambda tool: (
            _TOOL_SOURCE_ORDER[tool.source],
            (tool.plugin_name or tool.plugin_id or "").casefold(),
            (tool.plugin_id or "").casefold(),
            tool.display_name.casefold(),
            tool.name.casefold(),
        ),
    )
    choices: list[Choice] = []
    for tool in ordered:
        plugin_group = tool.plugin_name or tool.plugin_id
        choices.append(
            Choice(
                tool.name,
                tool.display_name,
                group=(
                    plugin_group if plugin_group is not None else _TOOL_SOURCE_GROUPS[tool.source]
                ),
                group_trailing=(
                    tool.plugin_id
                    if tool.plugin_id is not None and tool.plugin_name is not None
                    else ""
                ),
                trailing=_tool_labels(tool, theme=theme),
                disabled=tool.permission_control == "tool",
            )
        )
    return tuple(choices)


def _agent_definition_choices(
    definitions: tuple[AgentDefinitionView, ...],
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[Choice, ...]:
    return tuple(
        Choice(
            definition.agent_id,
            definition.name,
            group="BUILT-IN" if definition.source == "builtin" else "USER",
            trailing=theme.inline_separator.join((definition.agent_id, definition.compatibility)),
            disabled=not definition.editable,
        )
        for definition in sorted(
            definitions,
            key=lambda item: (item.source != "builtin", item.name.casefold()),
        )
    )


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


def _tool_labels(
    tool: TuiToolConfiguration,
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> str:
    permission: str
    if tool.permission_control == "tool":
        permission = PERMISSION_LABELS[tool.effective_permission or "unavailable"]
        return theme.inline_separator.join(("Plugin controlled", permission))
    elif tool.permission_override is not None:
        permission = PERMISSION_LABELS[tool.permission_override]
    elif tool.effective_permission is not None:
        permission = theme.inline_separator.join(
            ("Default", PERMISSION_LABELS[tool.effective_permission])
        )
    else:
        permission = "unavailable"
    availability = "" if tool.available else f"{theme.inline_separator}unavailable"
    return f"{permission}{availability}"


def _mcp_labels(
    config: MCPServerConfigView,
    status: McpServerStatus | None,
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> str:
    enabled = "enabled" if config.enabled else "disabled"
    runtime = status.status if status is not None else "next restart"
    return theme.inline_separator.join((enabled, runtime))


def _mcp_action_available(
    action: str,
    config: MCPServerConfigView,
    runtime: dict[str, McpServerStatus],
) -> bool:
    status = runtime.get(config.server_id)
    if action == "connect":
        return (
            config.enabled
            and status is not None
            and status.status not in {"connected", "starting", "reconnecting", "stopping"}
        )
    if action == "disconnect":
        return status is not None and status.status in {
            "connected",
            "starting",
            "reconnecting",
            "stopping",
        }
    return True


def _mcp_action_choices(
    action: str,
    configs: tuple[MCPServerConfigView, ...],
    runtime: dict[str, McpServerStatus],
    *,
    loading: set[str] | frozenset[str] = frozenset(),
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[Choice, ...]:
    return tuple(
        Choice(
            config.server_id,
            config.name,
            group=config.transport.upper(),
            trailing=_mcp_labels(config, runtime.get(config.server_id), theme=theme),
            disabled=(
                action in {"connect", "disconnect"}
                and not _mcp_action_available(action, config, runtime)
            ),
            loading=(
                config.server_id in loading or _mcp_status_is_loading(runtime.get(config.server_id))
            ),
        )
        for config in sorted(
            configs,
            key=lambda item: (item.transport, item.name.casefold()),
        )
    )


def _mcp_choices(
    configs: tuple[MCPServerConfigView, ...],
    runtime: dict[str, McpServerStatus],
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[Choice, ...]:
    return tuple(
        Choice(
            config.server_id,
            config.name,
            group=config.transport.upper(),
            trailing=_mcp_labels(config, runtime.get(config.server_id), theme=theme),
            loading=_mcp_status_is_loading(runtime.get(config.server_id)),
        )
        for config in sorted(
            configs,
            key=lambda item: (item.transport, item.name.casefold()),
        )
    )


def _mcp_management_choices(
    config: MCPServerConfigView,
    status: McpServerStatus | None,
    *,
    loading_action: str | None = None,
) -> tuple[Choice, ...]:
    runtime_status = status.status if status is not None else None
    if loading_action is None:
        if runtime_status in {"starting", "reconnecting"}:
            loading_action = "start"
        elif runtime_status == "stopping":
            loading_action = "stop"
    runtime_available = config.enabled and status is not None
    can_start = runtime_available and runtime_status in {"configured", "failed", "stopped"}
    can_manage_connection = runtime_available and runtime_status == "connected"
    can_stop = runtime_available and runtime_status in {
        "starting",
        "connected",
        "reconnecting",
    }

    return (
        Choice("edit", "Edit server"),
        Choice(
            "toggle",
            "Disable server" if config.enabled else "Enable server",
        ),
        Choice("check", "Check connection"),
        Choice(
            "start",
            "Connect",
            disabled=not can_start,
            loading=loading_action == "start",
        ),
        Choice(
            "refresh",
            "Refresh tools",
            disabled=not can_manage_connection,
            loading=loading_action == "refresh",
        ),
        Choice(
            "restart",
            "Reconnect",
            disabled=not can_manage_connection,
            loading=loading_action == "restart",
        ),
        Choice(
            "stop",
            "Disconnect",
            disabled=not can_stop,
            loading=loading_action == "stop",
        ),
        Choice("remove", "Delete server"),
    )


def _mcp_status_is_loading(status: McpServerStatus | None) -> bool:
    return status is not None and status.status in {"starting", "reconnecting", "stopping"}


def _skill_choice(
    skill: SkillView,
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> Choice:
    plugin_id = _plugin_skill_owner(skill)
    source_label = (
        "plugin"
        if skill.source == "plugin"
        else theme.inline_separator.join((skill.origin, skill.source))
    )
    return Choice(
        skill.name,
        skill.name,
        trailing=theme.inline_separator.join(
            (source_label, "enabled" if skill.enabled else "disabled")
        ),
        disabled=plugin_id is not None,
    )


def _skill_source_choices(
    policy: SkillPolicyView,
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[Choice, ...]:
    choices = [
        Choice(
            "discovery",
            "Find skills in folders",
            trailing="enabled" if policy.enabled else "disabled",
            group="FOLDERS",
        )
    ]
    choices.extend(
        Choice(
            source.source_id,
            theme.inline_separator.join((source.origin, source.scope)),
            trailing="enabled" if source.enabled else "disabled",
            group=f"{source.scope.upper()} FOLDERS",
        )
        for source in policy.sources
    )
    return tuple(choices)


def _skill_management_choices(skill: SkillView) -> tuple[Choice, ...]:
    return (
        Choice(
            "disable" if skill.enabled else "enable",
            "Disable" if skill.enabled else "Enable",
        ),
        Choice("validate", "Check skill"),
    )


def _plugin_skill_owner(skill: SkillView) -> str | None:
    ouro = skill.extensions.get("ouro")
    if not isinstance(ouro, dict):
        return None
    plugin_id = ouro.get("plugin_id")
    return plugin_id if isinstance(plugin_id, str) and plugin_id else None
