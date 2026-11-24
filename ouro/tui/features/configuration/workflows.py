from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from functools import partial
from typing import cast

from ouro.application.contracts.agents import AgentDefinitionCatalogView, AgentDefinitionView
from ouro.application.contracts.configuration import MCPServerConfigView, SkillPolicyView, SkillView
from ouro.config import QueuedInputConsumptionMode
from ouro.core.tools import PermissionMode
from ouro.tui.features.providers.model_menus import model_selection_label
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.confirmation import ConfirmationScreen
from ouro.tui.menus.models import Choice
from ouro.tui.menus.sorting import display_sort_key, sorted_agent_definitions
from ouro.tui.menus.workflows import WorkflowNavigation
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme
from ouro.tui.view_models import (
    McpServerStatus,
    TuiAgentDefinitionConfiguration,
    TuiAgentRuntimeConfiguration,
    TuiCompactionConfiguration,
    TuiMCPServerConfiguration,
    TuiSkillInstallConfiguration,
    TuiToolConfiguration,
    TuiToolExecutionConfiguration,
)


@dataclass(frozen=True)
class SystemSettingAction:
    """Stable menu-to-workflow contract; action IDs also back persisted recents."""

    action_id: str
    label: str
    group: str


SYSTEM_SETTING_ACTIONS = (
    SystemSettingAction("tasks.list", "Manage tasks", "Tasks"),
    SystemSettingAction("tasks.refresh", "Update status", "Tasks"),
    SystemSettingAction("tasks.create", "+ task", "Tasks"),
    SystemSettingAction("queued_inputs.delivery", "Queued messages", "Settings"),
    SystemSettingAction("compaction.policy", "Context compaction", "Settings"),
    SystemSettingAction("prompts.manage", "Prompts", "Settings"),
    SystemSettingAction("data.transfer", "Data", "Settings"),
    SystemSettingAction("tools.configure", "Tool", "Settings"),
    SystemSettingAction("providers.list", "Manage providers", "Providers"),
    SystemSettingAction("providers.endpoints.list", "Manage endpoints", "Providers"),
    SystemSettingAction(
        "providers.endpoints.use",
        "Set default endpoint",
        "Providers",
    ),
    SystemSettingAction("providers.add", "+ provider", "Providers"),
    SystemSettingAction("providers.endpoints.add", "+ endpoint", "Providers"),
    SystemSettingAction("plugins.list", "Manage plugins", "Plugins"),
    SystemSettingAction("plugins.install", "+ plugin", "Plugins"),
    SystemSettingAction("agents.policy", "Subagent permissions", "Subagents"),
    SystemSettingAction("agents.list", "Manage subagents", "Subagents"),
    SystemSettingAction("agents.create", "+ subagent", "Subagents"),
    SystemSettingAction("mcp.list", "Manage MCPs", "MCPs"),
    SystemSettingAction("mcp.add", "+ mcp", "MCPs"),
    SystemSettingAction("skills.sources", "Skill sources", "Skills"),
    SystemSettingAction("skills.list", "Manage skills", "Skills"),
    SystemSettingAction("skills.install", "+ skill", "Skills"),
)

_QUEUED_INPUT_CONSUMPTION_LABELS: dict[QueuedInputConsumptionMode, str] = {
    "one_by_one": "One at a time",
    "all_at_once": "All at once",
}
_QUEUED_INPUT_CONSUMPTION_DETAILS: dict[QueuedInputConsumptionMode, str] = {
    "one_by_one": "Process each message separately",
    "all_at_once": "Process waiting messages together",
}


class ConfigurationWorkflows:
    """Own the keyboard-first TUI workflows for durable configuration."""

    def __init__(
        self,
        navigation: WorkflowNavigation,
        *,
        sync_mcp_status: Callable[[], None],
        manage_plugin: Callable[..., Awaitable[None]],
    ) -> None:
        self._ui = navigation
        self._sync_mcp_status = sync_mcp_status
        self._manage_plugin = manage_plugin

    async def _configure_queued_inputs(self, *, menu: ChoiceScreen | None = None) -> None:
        try:
            config = await self._ui.controller().queued_input_configuration()
        except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
            self._ui.notify_error(exc, title="Queued message settings")
            return

        def choices(selected: QueuedInputConsumptionMode) -> tuple[Choice, ...]:
            return tuple(
                Choice(
                    mode,
                    label,
                    trailing=_QUEUED_INPUT_CONSUMPTION_DETAILS[mode],
                    marker=(self._ui.theme().selected_choice_marker if mode == selected else ""),
                )
                for mode, label in _QUEUED_INPUT_CONSUMPTION_LABELS.items()
            )

        async def select_mode(value: str) -> tuple[Choice, ...] | None:
            try:
                mode = cast(QueuedInputConsumptionMode, value)
                saved = await self._ui.controller().update_queued_input_consumption(mode)
            except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
                self._ui.notify_error(exc, title="Queued message settings")
                return None
            self._ui.notify(
                "Waiting messages will be processed "
                f"{_QUEUED_INPUT_CONSUMPTION_LABELS[saved.consumption_mode].lower()}.",
                markup=False,
            )
            return choices(saved.consumption_mode)

        if menu is not None:
            menu.push_page(
                title="QUEUED MESSAGES",
                placeholder=None,
                choices=choices(config.consumption_mode),
                initial_value=config.consumption_mode,
                on_select=select_mode,
            )
            return
        await self._ui.choose(
            ChoiceScreen(
                title="QUEUED MESSAGES",
                placeholder=None,
                choices=choices(config.consumption_mode),
                initial_value=config.consumption_mode,
                on_select=select_mode,
            )
        )

    async def _configure_compaction(self, *, menu: ChoiceScreen | None = None) -> None:
        from ouro.tui.features.configuration.compaction import CompactionConfigurationScreen

        try:
            config = await self._ui.controller().compaction_configuration()
            models = await self._ui.controller().models()
            strategies = self._ui.controller().compaction_strategies()
        except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
            self._ui.notify_error(exc, title="Conversation compaction settings")
            return
        screen = CompactionConfigurationScreen(
            config,
            models=models,
            strategy_ids=strategies,
            theme=self._ui.theme(),
            system_default_model=await self._ui.controller().configured_default_model(),
        )
        draft = await (menu.open_child(screen) if menu is not None else self._ui.choose(screen))
        if draft is None:
            return
        compaction_draft = cast(TuiCompactionConfiguration, draft)
        try:
            saved = await self._ui.controller().save_compaction_configuration(compaction_draft)
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            self._ui.notify_error(exc, title="Conversation compaction settings")
            return
        route = (
            f"{saved.model.provider_id}/{saved.model.endpoint_id}"
            if saved.model.has_dedicated_endpoint
            else None
        )
        label = (
            "default model"
            if saved.model.use_default_model
            else model_selection_label(route, models, theme=self._ui.theme())
            if route
            else "conversation model"
        )
        self._ui.notify(
            f"Compaction settings saved for the next request. Model: {label}.", markup=False
        )

    async def _configure_tools(self, *, menu: ChoiceScreen | None = None) -> None:
        from ouro.tui.features.configuration.tools import ToolConfigurationScreen

        try:
            controller = self._ui.controller()
            current = await controller.tool_execution_configuration()
            tools = await controller.tool_configurations()
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Tool settings")
            return

        async def save_permission(
            name: str, mode: PermissionMode | None
        ) -> tuple[TuiToolConfiguration, ...] | None:
            try:
                await controller.set_tool_permission(name, mode)
                return await controller.tool_configurations()
            except Exception as exc:  # noqa: BLE001
                self._ui.notify_error(exc, title="Could not change tool permission")
                return None

        screen = ToolConfigurationScreen(current, tools=tools, on_permission=save_permission)
        draft = await (menu.open_child(screen) if menu is not None else self._ui.choose(screen))
        if draft is None:
            return
        try:
            updated = await controller.update_tool_execution(
                cast(TuiToolExecutionConfiguration, draft)
            )
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Tool settings")
            return
        self._ui.notify(
            f"Tool settings saved. The next group of tools will use {updated.mode} mode.",
            markup=False,
        )

    async def _configure_agent_policy(self, *, menu: ChoiceScreen | None = None) -> None:
        from ouro.tui.features.configuration.agents import AgentDispatchPolicyScreen

        try:
            current = await self._ui.controller().agent_runtime_configuration()
            models = await self._ui.controller().models()
        except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
            self._ui.notify_error(exc, title="Subagent settings")
            return
        screen = AgentDispatchPolicyScreen(
            current,
            models=models,
            theme=self._ui.theme(),
        )
        draft = await (menu.open_child(screen) if menu is not None else self._ui.choose(screen))
        if draft is None:
            return
        try:
            await self._ui.controller().update_agent_runtime(
                cast(TuiAgentRuntimeConfiguration, draft)
            )
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            self._ui.notify_error(exc, title="Subagent settings")
            return

    async def _choose_agent_definitions(
        self,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        try:
            catalog = await self._ui.controller().agent_definition_catalog()
        except Exception as exc:  # noqa: BLE001 - catalog errors are user-visible.
            self._ui.notify_error(exc, title="Could not load subagents")
            return
        screen: ChoiceScreen

        async def refresh_definitions() -> tuple[Choice, ...] | None:
            try:
                refreshed = await self._ui.controller().agent_definition_catalog()
            except Exception as exc:  # noqa: BLE001
                self._ui.notify_error(exc, title="Could not load subagents")
                return None
            return _agent_definition_choices(refreshed.definitions, theme=self._ui.theme())

        async def select_definition(agent_id: str) -> tuple[Choice, ...] | None:
            try:
                current = await self._ui.controller().agent_definition_catalog()
            except Exception as exc:  # noqa: BLE001 - catalog errors are user-visible.
                self._ui.notify_error(exc, title="Could not load subagents")
                return None
            definition = next(
                (item for item in current.definitions if item.agent_id == agent_id),
                None,
            )
            if definition is None:
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
                description=_agent_catalog_notice(catalog),
                placeholder="Filter subagents…",
                choices=_agent_definition_choices(catalog.definitions, theme=self._ui.theme()),
                on_select=select_definition,
                on_refresh=refresh_definitions,
            )
            return
        screen = ChoiceScreen(
            title="SUBAGENTS",
            description=_agent_catalog_notice(catalog),
            placeholder="Filter subagents…",
            choices=_agent_definition_choices(catalog.definitions, theme=self._ui.theme()),
            on_select=select_definition,
            on_refresh=refresh_definitions,
        )
        await self._ui.choose(screen)

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
            elif action == "delete":
                saved = await self._delete_agent_definition(
                    definition.agent_id,
                    expected_revision=expected_revision,
                    menu=menu,
                )
            elif action == "toggle":
                try:
                    saved = await self._ui.controller().set_agent_definition_enabled(
                        definition.agent_id,
                        enabled=not definition.enabled,
                        expected_revision=expected_revision,
                    )
                except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
                    self._ui.notify_error(exc, title="Could not update the subagent")
                    return None
            else:
                return None
            if saved is not None:
                menu.pop_page(
                    choices=_agent_definition_choices(saved.definitions, theme=self._ui.theme()),
                    initial_value=definition.agent_id,
                )
            return None

        menu.push_page(
            title=f"SUBAGENT{self._ui.theme().inline_separator}{definition.name}",
            description=(
                "Current dispatch policy does not allow this subagent. "
                "Enabling it does not override policy; adjust framework configuration or CLI."
                if not definition.allowed_by_policy
                else ""
            ),
            placeholder=None,
            choices=(
                Choice("edit", "Edit", disabled=not definition.editable),
                Choice("toggle", "Disable" if definition.enabled else "Enable"),
                Choice(
                    "delete",
                    "Delete",
                    disabled=not definition.editable,
                    accent=self._ui.theme().danger if definition.editable else None,
                    destructive=definition.editable,
                ),
            ),
            on_select=select_action,
        )

    async def _create_agent_definition(self, *, menu: ChoiceScreen | None = None) -> None:
        from ouro.tui.features.configuration.agents import AgentDefinitionConfigurationScreen

        try:
            catalog = await self._ui.controller().agent_definition_catalog()
            tools = await self._ui.controller().agent_definition_tools()
            models = await self._ui.controller().models()
        except Exception as exc:  # noqa: BLE001 - catalog errors are user-visible.
            self._ui.notify_error(exc, title="Could not load subagent settings")
            return
        screen = AgentDefinitionConfigurationScreen(
            tools,
            models,
            theme=self._ui.theme(),
            system_default_model=await self._ui.controller().configured_default_model(),
        )
        draft = await (menu.open_child(screen) if menu is not None else self._ui.choose(screen))
        if draft is None:
            return
        definition = cast(TuiAgentDefinitionConfiguration, draft)
        try:
            await self._ui.controller().create_agent_definition(
                definition,
                expected_revision=catalog.revision,
            )
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            self._ui.notify_error(exc, title="Could not create the subagent")
            return

    async def _edit_agent_definition(
        self,
        agent_id: str,
        *,
        expected_revision: int,
        menu: ChoiceScreen | None = None,
    ) -> AgentDefinitionCatalogView | None:
        from ouro.tui.features.configuration.agents import AgentDefinitionConfigurationScreen

        try:
            existing = await self._ui.controller().agent_definition(agent_id)
            tools = await self._ui.controller().agent_definition_tools()
            models = await self._ui.controller().models()
        except Exception as exc:  # noqa: BLE001 - catalog errors are user-visible.
            self._ui.notify_error(exc, title="Could not load subagent settings")
            return None
        screen = AgentDefinitionConfigurationScreen(
            tools,
            models,
            existing,
            theme=self._ui.theme(),
            system_default_model=await self._ui.controller().configured_default_model(),
        )
        draft = await (menu.open_child(screen) if menu is not None else self._ui.choose(screen))
        if draft is None:
            return None
        definition = cast(TuiAgentDefinitionConfiguration, draft)
        try:
            saved = await self._ui.controller().update_agent_definition(
                agent_id,
                definition,
                expected_revision=expected_revision,
            )
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            self._ui.notify_error(exc, title="Could not save subagent settings")
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
                f"Delete {agent_id}? Current tasks can finish. New tasks cannot use this subagent."
            ),
            confirm_label="Delete",
        )
        selected = await (menu.open_child(screen) if menu is not None else self._ui.choose(screen))
        if not ConfirmationScreen.confirmed(selected):
            return None
        try:
            saved = await self._ui.controller().delete_agent_definition(
                agent_id,
                expected_revision=expected_revision,
            )
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            self._ui.notify_error(exc, title="Could not delete the subagent")
            return None
        return saved

    async def _choose_mcp(self, *, menu: ChoiceScreen | None = None) -> None:
        async def refresh_servers() -> tuple[Choice, ...] | None:
            try:
                configs = await self._ui.controller().mcp_configurations()
                self._sync_mcp_status()
                runtime = {item.server_id: item for item in self._ui.controller().mcp_servers()}
            except Exception as exc:  # noqa: BLE001
                self._ui.notify_error(exc, title="Could not load MCP servers")
                return None
            return _mcp_choices(configs, runtime, theme=self._ui.theme())

        choices = await refresh_servers()
        if choices is None:
            return
        if menu is not None:

            async def select_server(server_id: str) -> tuple[Choice, ...] | None:
                if server_id == "+":
                    await self._edit_mcp(None, menu=menu)
                else:
                    await self._manage_mcp(server_id, menu=menu)
                return None

            menu.push_page(
                title="MCPs",
                placeholder="Filter MCPs…",
                choices=choices,
                on_select=select_server,
                on_refresh=refresh_servers,
            )
            return
        selected = await self._ui.choose(
            ChoiceScreen(title="MCPs", placeholder="Filter MCPs…", choices=choices)
        )
        if selected == "+":
            await self._edit_mcp(None)
        elif selected is not None:
            await self._manage_mcp(selected)

    async def _manage_mcp(
        self,
        server_id: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        try:
            config = await self._ui.controller().mcp_configuration(server_id)
            self._sync_mcp_status()
            status = next(
                (
                    item
                    for item in self._ui.controller().mcp_servers()
                    if item.server_id == server_id
                ),
                None,
            )
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Could not load MCP servers")
            return

        actions = _mcp_management_choices(config, status, theme=self._ui.theme())

        if menu is not None:
            loading_action: str | None = None

            async def refresh_mcp_actions() -> tuple[Choice, ...] | None:
                nonlocal config, status
                try:
                    config = await self._ui.controller().mcp_configuration(server_id)
                    self._sync_mcp_status()
                    status = next(
                        (
                            item
                            for item in self._ui.controller().mcp_servers()
                            if item.server_id == server_id
                        ),
                        None,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._ui.notify_error(exc, title="Could not load MCP servers")
                    return None
                return _mcp_management_choices(
                    config,
                    status,
                    loading_action=loading_action,
                    theme=self._ui.theme(),
                )

            async def finish_runtime_action(action: str, page_revision: int) -> None:
                nonlocal config, status, loading_action
                try:
                    await self._run_mcp_action(server_id, action)
                finally:
                    loading_action = None
                try:
                    configs = await self._ui.controller().mcp_configurations()
                    runtime = {item.server_id: item for item in self._ui.controller().mcp_servers()}
                    config = next(item for item in configs if item.server_id == server_id)
                    status = runtime.get(server_id)
                except Exception as exc:  # noqa: BLE001
                    self._ui.notify_error(exc, title="Could not load MCP servers")
                    return
                if menu.update_choices(
                    _mcp_management_choices(config, status, theme=self._ui.theme()),
                    expected_revision=page_revision,
                    preferred_value=action,
                ):
                    menu.update_parent_choices(
                        _mcp_choices(configs, runtime, theme=self._ui.theme()),
                        initial_value=server_id,
                    )
                else:
                    await menu.refresh_choices()

            async def select_action(selected: str) -> tuple[Choice, ...] | None:
                nonlocal config, status, loading_action
                if selected == "edit":
                    saved = await self._edit_mcp(config, menu=menu)
                    if saved is not None:
                        config = saved
                        menu.update_choices(
                            _mcp_management_choices(config, status, theme=self._ui.theme()),
                            title=f"MCP{self._ui.theme().inline_separator}{config.name}",
                        )
                elif selected == "toggle":
                    if not await self._set_mcp_enabled(
                        server_id,
                        enabled=not config.enabled,
                    ):
                        return None
                    configs = await self._ui.controller().mcp_configurations()
                    runtime = {item.server_id: item for item in self._ui.controller().mcp_servers()}
                    config = next(item for item in configs if item.server_id == server_id)
                    status = runtime.get(server_id)
                    menu.update_parent_choices(
                        _mcp_choices(configs, runtime, theme=self._ui.theme()),
                        initial_value=server_id,
                    )
                    return _mcp_management_choices(config, status, theme=self._ui.theme())
                elif selected == "check":
                    await self._check_mcp(server_id)
                elif selected in {"start", "stop", "restart", "refresh"}:
                    if loading_action is not None:
                        return _mcp_management_choices(
                            config,
                            status,
                            loading_action=loading_action,
                            theme=self._ui.theme(),
                        )
                    loading_action = selected
                    self._ui.start(
                        partial(finish_runtime_action, selected, menu.page_revision),
                        group=f"mcp-menu-{server_id}",
                    )
                    return _mcp_management_choices(
                        config,
                        status,
                        loading_action=loading_action,
                        theme=self._ui.theme(),
                    )
                elif selected == "remove":
                    if await self._remove_mcp(config, menu=menu):
                        await menu.pop_page_refreshed()
                return None

            menu.push_page(
                title=f"MCP{self._ui.theme().inline_separator}{config.name}",
                placeholder=None,
                choices=actions,
                on_select=select_action,
                on_refresh=refresh_mcp_actions,
            )
            return
        selected = await self._ui.choose(
            ChoiceScreen(
                title=f"MCP{self._ui.theme().inline_separator}{config.name}",
                placeholder=None,
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
            await self._remove_mcp(config)

    async def _edit_mcp(
        self,
        existing: MCPServerConfigView | None,
        *,
        menu: ChoiceScreen | None = None,
    ) -> MCPServerConfigView | None:
        from ouro.tui.features.configuration.mcp import MCPConfigurationScreen

        screen = MCPConfigurationScreen(existing, theme=self._ui.theme())
        draft = await (menu.open_child(screen) if menu is not None else self._ui.choose(screen))
        if draft is None:
            return None
        mcp_draft = cast(TuiMCPServerConfiguration, draft)
        try:
            saved = await self._ui.controller().save_mcp_configuration(mcp_draft)
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Could not save MCP settings")
            return None
        self._sync_mcp_status()
        self._ui.notify(
            f"Saved settings for {saved.name}. Tool permissions apply now. "
            "Restart OURO to apply connection changes.",
            markup=False,
        )
        return saved

    async def _check_mcp(self, server_id: str) -> None:
        try:
            with self._ui.loading("check"):
                result = await self._ui.controller().check_mcp_server(server_id)
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Could not check the MCP connection")
            return
        if result.ok:
            self._ui.notify(
                f"Connected to {result.server.name}. Tools found: {len(result.tool_names)}.",
                markup=False,
            )
        else:
            self._ui.notify_error(
                f"Could not verify the connection to {result.server.name}: "
                f"{result.error or result.status}"
            )

    async def _run_mcp_action(
        self,
        server_id: str,
        action: str,
    ) -> McpServerStatus | None:
        operations = {
            "start": self._ui.controller().start_mcp_server,
            "stop": self._ui.controller().stop_mcp_server,
            "restart": self._ui.controller().restart_mcp_server,
            "refresh": self._ui.controller().refresh_mcp_tools,
        }
        try:
            status = await operations[action](server_id)
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="MCP server action failed")
            return None
        self._sync_mcp_status()
        if status.status == "failed":
            self._ui.notify_error(f"MCP {status.name}: {status.error or status.status}")
        return status

    async def _remove_mcp(
        self,
        config: MCPServerConfigView,
        *,
        menu: ChoiceScreen | None = None,
    ) -> bool:
        screen = ConfirmationScreen(
            title="DELETE MCP",
            message=f"Remove {config.name} from your MCP servers after the next restart?",
            confirm_label="Delete",
        )
        selected = await (menu.open_child(screen) if menu is not None else self._ui.choose(screen))
        if not ConfirmationScreen.confirmed(selected):
            return False
        try:
            await self._ui.controller().remove_mcp_configuration(config.server_id)
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Could not remove the MCP server")
            return False
        self._sync_mcp_status()
        self._ui.notify(
            f"Removed {config.name} from settings. Restart OURO to apply the change.", markup=False
        )
        return True

    async def _set_mcp_enabled(self, server_id: str, *, enabled: bool) -> bool:
        try:
            await self._ui.controller().set_mcp_enabled(server_id, enabled=enabled)
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Could not change the MCP server status")
            return False
        self._sync_mcp_status()
        return True

    async def _configure_skill_sources(
        self,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        try:
            policy = await self._ui.controller().skill_policy()
        except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
            self._ui.notify_error(exc, title="Could not load skill sources")
            return

        async def refresh_sources() -> tuple[Choice, ...] | None:
            nonlocal policy
            try:
                policy = await self._ui.controller().skill_policy()
            except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
                self._ui.notify_error(exc, title="Could not load skill sources")
                return None
            return _skill_source_choices(policy)

        async def toggle_source(source_id: str) -> tuple[Choice, ...] | None:
            nonlocal policy
            try:
                if source_id == "discovery":
                    policy = await self._ui.controller().update_skill_discovery(
                        enabled=not policy.enabled
                    )
                else:
                    source = next(
                        (item for item in policy.sources if item.source_id == source_id),
                        None,
                    )
                    if source is None:
                        return None
                    policy = await self._ui.controller().update_skill_discovery(
                        source_updates={source_id: not source.enabled}
                    )
            except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
                self._ui.notify_error(exc, title="Could not save skill sources")
                return None
            return _skill_source_choices(policy)

        choices = _skill_source_choices(policy)
        hint = self._ui.theme().inline_separator.join(
            (
                "↑/↓ move",
                "←/→ group",
                "Enter toggle",
                "Esc back" if menu is not None else "Esc close",
            )
        )
        if menu is not None:
            menu.push_page(
                title="SKILL SOURCES",
                placeholder=None,
                choices=choices,
                on_select=toggle_source,
                on_refresh=refresh_sources,
                interaction_hint=hint,
            )
            return
        await self._ui.choose(
            ChoiceScreen(
                title="SKILL SOURCES",
                placeholder=None,
                choices=choices,
                on_select=toggle_source,
                on_refresh=refresh_sources,
                interaction_hint=hint,
            )
        )

    async def _choose_skills(
        self,
        selector: str | None = None,
        *,
        menu: ChoiceScreen | None = None,
        switch: bool = False,
    ) -> None:
        revision = menu.page_revision if menu is not None else None
        try:
            with self._ui.loading(None):
                catalog = await self._ui.controller().skill_catalog()
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Could not load skills")
            return
        if menu is not None and (not menu.is_attached or menu.page_revision != revision):
            return
        if selector is None:
            choices = _skill_choices(catalog.skills, theme=self._ui.theme())
            skill_menu: ChoiceScreen
            skills = {skill.name: skill for skill in catalog.skills}

            async def refresh_skills() -> tuple[Choice, ...] | None:
                try:
                    with self._ui.loading(None):
                        refreshed = await self._ui.controller().skill_catalog()
                except Exception as exc:  # noqa: BLE001
                    self._ui.notify_error(exc, title="Could not load skills")
                    return None
                skills.clear()
                skills.update({skill.name: skill for skill in refreshed.skills})
                return _skill_choices(refreshed.skills, theme=self._ui.theme())

            async def select_skill(name: str) -> tuple[Choice, ...] | None:
                if name not in skills:
                    return None
                await self._manage_skill(name, menu=skill_menu)
                return None

            if menu is not None:
                skill_menu = menu
                navigate = menu.switch_page if switch else menu.push_page
                navigate(
                    navigation_id="skills",
                    title="SKILLS",
                    placeholder="Filter skills…",
                    choices=choices,
                    on_select=select_skill,
                    on_refresh=refresh_skills,
                )
                return
            skill_menu = ChoiceScreen(
                navigation_id="skills",
                title="SKILLS",
                placeholder="Filter skills…",
                choices=choices,
                on_select=select_skill,
                on_refresh=refresh_skills,
            )
            self._ui.show(skill_menu)
            return
        if not any(item.name == selector for item in catalog.skills):
            self._ui.notify_error(f"Skill not found: {selector}")
            return
        await self._manage_skill(selector)

    async def _choose_skill_action(
        self,
        action: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        if action not in {"toggle", "update", "remove"}:
            self._ui.notify_error(f"Unknown skill action: {action}")
            return
        try:
            with self._ui.loading(None):
                catalog = await self._ui.controller().skill_catalog()
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Could not load skills")
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
            "update": "UPDATE",
            "remove": "DELETE",
        }[action]
        choices = tuple(_skill_choice(skill, theme=self._ui.theme()) for skill in candidates)
        if action == "toggle":
            skills = {skill.name: skill for skill in candidates}

            async def toggle_skill(name: str) -> tuple[Choice, ...] | None:
                skill = skills[name]
                if not await self._set_skill_enabled(name, enabled=not skill.enabled):
                    return None
                try:
                    with self._ui.loading(None):
                        refreshed = await self._ui.controller().skill_catalog()
                except Exception as exc:  # noqa: BLE001
                    self._ui.notify_error(exc, title="Skill settings")
                    return None
                skills.clear()
                skills.update({skill.name: skill for skill in refreshed.skills})
                return tuple(
                    _skill_choice(skill, theme=self._ui.theme()) for skill in skills.values()
                )

            if menu is not None:
                menu.push_page(
                    title=f"{action_label} SKILL",
                    placeholder="Filter skills…",
                    choices=choices,
                    on_select=toggle_skill,
                )
            else:
                await self._ui.choose(
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
                    with self._ui.loading(None):
                        refreshed = await self._ui.controller().skill_catalog()
                except Exception as exc:  # noqa: BLE001
                    self._ui.notify_error(exc, title="Could not load skills")
                    return None
                refreshed_candidates = (
                    tuple(skill for skill in refreshed.skills if skill.managed)
                    if action in {"update", "remove"}
                    else refreshed.skills
                )
                skills.clear()
                skills.update({skill.name: skill for skill in refreshed_candidates})
                return tuple(
                    _skill_choice(skill, theme=self._ui.theme()) for skill in refreshed_candidates
                )

            async def select_skill(name: str) -> tuple[Choice, ...] | None:
                skill = skills[name]
                if action == "update":
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
        selected = await self._ui.choose(
            ChoiceScreen(
                title=f"{action_label} SKILL",
                placeholder="Filter skills…",
                choices=choices,
            )
        )
        if selected is None:
            return
        skill = next(item for item in candidates if item.name == selected)
        if action == "update":
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
            with self._ui.loading(None):
                catalog = await self._ui.controller().skill_catalog()
            skill = next(
                (item for item in catalog.skills if item.name == name),
                None,
            )
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Could not load skills")
            return
        if skill is None:
            self._ui.notify_error(f"Skill not found: {name}")
            return
        plugin_id = _plugin_skill_owner(skill)
        if plugin_id is not None:
            await self._manage_plugin(plugin_id, menu=menu)
            return

        actions = _skill_management_choices(skill)
        if menu is not None:

            async def refresh_skill_actions() -> tuple[Choice, ...] | None:
                nonlocal skill
                try:
                    with self._ui.loading(None):
                        catalog = await self._ui.controller().skill_catalog()
                except Exception as exc:  # noqa: BLE001
                    self._ui.notify_error(exc, title="Could not load skills")
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
                    with self._ui.loading(None):
                        catalog = await self._ui.controller().skill_catalog()
                    refreshed = next(
                        (item for item in catalog.skills if item.name == name),
                        None,
                    )
                    if refreshed is None:
                        self._ui.notify_error(f"The skill {name} is no longer listed.")
                        return None
                    skill = refreshed
                    menu.update_parent_choices(
                        _skill_choices(catalog.skills, theme=self._ui.theme()),
                        initial_value=name,
                    )
                    return _skill_management_choices(skill)
                elif selected == "update":
                    await self._update_skill(name)
                elif selected == "remove":
                    await self._remove_skill(name, menu=menu)
                return None

            menu.push_page(
                title=f"SKILL{self._ui.theme().inline_separator}{name}",
                description=f"{skill.description}\n\n{skill.location}",
                placeholder=None,
                choices=actions,
                on_select=select_action,
                on_refresh=refresh_skill_actions,
            )
            return
        selected = await self._ui.choose(
            ChoiceScreen(
                title=f"SKILL{self._ui.theme().inline_separator}{name}",
                description=f"{skill.description}\n\n{skill.location}",
                placeholder=None,
                choices=actions,
            )
        )
        if selected in {"enable", "disable"}:
            await self._set_skill_enabled(name, enabled=selected == "enable")
        elif selected == "update":
            await self._update_skill(name)
        elif selected == "remove":
            await self._remove_skill(name)

    async def _install_skill(self, *, menu: ChoiceScreen | None = None) -> None:
        from ouro.tui.features.configuration.skills import SkillInstallScreen

        screen = SkillInstallScreen()
        draft = await (menu.open_child(screen) if menu is not None else self._ui.choose(screen))
        if draft is None:
            return
        skill_draft = cast(TuiSkillInstallConfiguration, draft)
        try:
            with self._ui.loading(None):
                result = await self._ui.controller().install_skills(skill_draft)
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Could not install skills")
            return
        names = ", ".join(item.name for item in result.skills)
        self._ui.notify(
            f"Installed skills: {names}" if names else "No skills were installed.", markup=False
        )

    async def _set_skill_enabled(self, name: str, *, enabled: bool) -> bool:
        try:
            await self._ui.controller().set_skill_enabled(name, enabled=enabled)
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Could not change the skill status")
            return False
        return True

    async def _update_skill(self, name: str) -> None:
        try:
            with self._ui.loading("update"):
                await self._ui.controller().update_skill(name)
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Could not update the skill")
            return
        self._ui.notify(f"Updated {name}. Restart OURO to use the updated skill.", markup=False)

    async def _remove_skill(
        self,
        name: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        screen = ConfirmationScreen(
            title="DELETE SKILL",
            message=f"Delete the skill {name}? The change takes effect when OURO restarts.",
            confirm_label="Delete",
        )
        selected = await (menu.open_child(screen) if menu is not None else self._ui.choose(screen))
        if not ConfirmationScreen.confirmed(selected):
            return
        try:
            with self._ui.loading("remove"):
                await self._ui.controller().remove_skill(name)
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Could not delete the skill")
            return
        self._ui.notify(
            f"Deleted the skill {name}. Restart OURO to apply the change.", markup=False
        )


def _agent_catalog_notice(catalog: AgentDefinitionCatalogView) -> str:
    return (
        "Restart OURO to apply the new subagent settings. Status shows the settings in use."
        if catalog.policy_restart_required
        else ""
    )


def _agent_definition_choices(
    definitions: tuple[AgentDefinitionView, ...],
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[Choice, ...]:
    from ouro.tui.features.configuration.agents import agent_definition_status

    return tuple(
        Choice(
            definition.agent_id,
            definition.name,
            group="BUILT-IN" if definition.source == "builtin" else "USER",
            trailing=agent_definition_status(definition, theme=theme),
            enabled_state=definition.enabled,
        )
        for definition in sorted_agent_definitions(definitions)
    )


def _mcp_labels(status: McpServerStatus | None) -> str:
    if status is None:
        return "next restart"
    return "" if status.status == "disabled" else status.status


def _mcp_choices(
    configs: tuple[MCPServerConfigView, ...],
    runtime: dict[str, McpServerStatus],
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[Choice, ...]:
    choices = tuple(
        Choice(
            config.server_id,
            config.name,
            enabled_state=config.enabled,
            group=config.transport.upper(),
            trailing=_mcp_labels(runtime.get(config.server_id)),
            loading=_mcp_status_is_loading(runtime.get(config.server_id)),
        )
        for config in sorted(
            configs,
            key=lambda item: (item.transport, display_sort_key(item.name, fallback=item.server_id)),
        )
    )
    return (*choices, Choice("+", "+"))


def _mcp_management_choices(
    config: MCPServerConfigView,
    status: McpServerStatus | None,
    *,
    loading_action: str | None = None,
    theme: TuiTheme = DEFAULT_TUI_THEME,
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
        Choice("edit", "Edit"),
        Choice(
            "toggle",
            "Disable" if config.enabled else "Enable",
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
        Choice("remove", "Delete", accent=theme.danger, destructive=True),
    )


def _mcp_status_is_loading(status: McpServerStatus | None) -> bool:
    return status is not None and status.status in {"starting", "reconnecting", "stopping"}


def _skill_choice(
    skill: SkillView,
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> Choice:
    return Choice(
        skill.name,
        skill.name,
        trailing=_skill_source_label(skill, theme=theme),
        enabled_state=skill.enabled,
    )


def _skill_source_label(skill: SkillView, *, theme: TuiTheme) -> str:
    if skill.source == "plugin":
        owner = skill.extensions.get("ouro")
        name = owner.get("plugin_name") if isinstance(owner, dict) else None
        if not isinstance(name, str) or not name:
            name = _plugin_skill_owner(skill)
        return theme.inline_separator.join((name, "plugin")) if name else "plugin"
    return theme.inline_separator.join((skill.origin, skill.source))


def _skill_choices(
    skills: Iterable[SkillView], *, theme: TuiTheme = DEFAULT_TUI_THEME
) -> tuple[Choice, ...]:
    return tuple(
        _skill_choice(skill, theme=theme)
        for skill in sorted(
            skills,
            key=lambda skill: (
                skill.source == "plugin",
                _skill_source_label(skill, theme=theme).casefold(),
                display_sort_key(skill.name, fallback=skill.name),
            ),
        )
    )


def _skill_source_choices(policy: SkillPolicyView) -> tuple[Choice, ...]:
    choices = [
        Choice(
            "discovery",
            "Discover skills in folders",
            enabled_state=policy.enabled,
            group="GENERAL",
        )
    ]
    choices.extend(
        Choice(
            source.source_id,
            source.origin,
            enabled_state=source.enabled,
            group=source.scope.upper(),
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
    )


def _plugin_skill_owner(skill: SkillView) -> str | None:
    ouro = skill.extensions.get("ouro")
    if not isinstance(ouro, dict):
        return None
    plugin_id = ouro.get("plugin_id")
    return plugin_id if isinstance(plugin_id, str) and plugin_id else None
