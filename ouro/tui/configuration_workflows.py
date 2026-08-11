from __future__ import annotations

from dataclasses import dataclass

from textual import work
from textual.app import App

from ouro.application.configuration_models import MCPServerConfigView, SkillView
from ouro.core.tools import PermissionMode
from ouro.tui.configuration_screens import (
    CompactionConfigurationScreen,
    ConfirmScreen,
    MCPConfigurationScreen,
    SkillInstallScreen,
    ToolExecutionConfigurationScreen,
)
from ouro.tui.controller import (
    McpServerStatus,
    TuiController,
    TuiSystemStatus,
    TuiToolConfiguration,
    project_mcp_system_status,
)
from ouro.tui.screens import Choice, ChoiceScreen
from ouro.tui.widgets import ConversationView


@dataclass(frozen=True)
class SystemSettingAction:
    """Stable menu-to-workflow contract; action IDs also back persisted recents."""

    action_id: str
    label: str
    group: str


SYSTEM_SETTING_ACTIONS = (
    SystemSettingAction("tools.permissions", "Tool permissions", "Tools"),
    SystemSettingAction("tools.execution", "Execution & path access", "Tools"),
    SystemSettingAction("mcp.list", "List MCP servers", "MCP"),
    SystemSettingAction("mcp.add", "Add MCP server", "MCP"),
    SystemSettingAction("mcp.connect", "Connect MCP server", "MCP"),
    SystemSettingAction("mcp.disconnect", "Disconnect MCP server", "MCP"),
    SystemSettingAction("mcp.edit", "Edit MCP server", "MCP"),
    SystemSettingAction("mcp.remove", "Remove MCP server", "MCP"),
    SystemSettingAction("skills.list", "List skills", "Skills"),
    SystemSettingAction("skills.install", "Install skill", "Skills"),
    SystemSettingAction("skills.toggle", "Enable or disable skill", "Skills"),
    SystemSettingAction("skills.validate", "Validate skill", "Skills"),
    SystemSettingAction("skills.update", "Update skill", "Skills"),
    SystemSettingAction("skills.remove", "Remove skill", "Skills"),
    SystemSettingAction("compaction.policy", "Compaction policy", "Compaction"),
)

_TOOL_PERMISSION_CHOICES: dict[str, PermissionMode | None] = {
    "inherit": None,
    "auto": "auto",
    "approval": "approval",
    "deny": "deny",
}


class ConfigurationWorkflowsApp(App[int]):
    """Own the keyboard-first TUI workflows for durable configuration."""

    _controller: TuiController
    _system_status: TuiSystemStatus

    @work(exclusive=True, group="configuration-navigation")
    async def _run_system_setting(self, action_id: str) -> None:
        if action_id == "tools.permissions":
            self._choose_tools()
        elif action_id == "tools.execution":
            await self._configure_tool_execution()
        elif action_id == "mcp.list":
            self._choose_mcp()
        elif action_id == "mcp.add":
            await self._edit_mcp(None)
        elif action_id.startswith("mcp."):
            await self._choose_mcp_action(action_id.removeprefix("mcp."))
        elif action_id == "skills.list":
            self._choose_skills()
        elif action_id == "skills.install":
            await self._install_skill()
        elif action_id.startswith("skills."):
            await self._choose_skill_action(action_id.removeprefix("skills."))
        elif action_id == "compaction.policy":
            self._configure_compaction()
        else:
            available = "|".join(action.action_id for action in SYSTEM_SETTING_ACTIONS)
            await self.query_one(ConversationView).add_notice(
                f"Unknown system setting action: {action_id}. Available: {available}",
                error=True,
            )

    @work(exclusive=True, group="configuration")
    async def _configure_compaction(self) -> None:
        try:
            config = await self._controller.compaction_configuration()
            models = await self._controller.models()
            strategies = self._controller.compaction_strategies()
        except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        draft = await self.push_screen_wait(
            CompactionConfigurationScreen(
                config,
                models=models,
                strategy_ids=strategies,
            )
        )
        if draft is None:
            return
        try:
            saved = await self._controller.save_compaction_configuration(draft)
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        route = (
            f"{saved.model.provider_id}/{saved.model.endpoint_id}"
            if saved.model.has_dedicated_endpoint
            else "session runtime model"
        )
        self.notify(f"Compaction options saved: {route} (next restart)")

    @work(exclusive=True, group="configuration")
    async def _choose_tools(self, selector: str | None = None) -> None:
        try:
            tools = await self._controller.tool_configurations()
        except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        selected = selector
        if selected is None:
            choices = tuple(
                Choice(item.name, item.display_name, _tool_description(item)) for item in tools
            )
            if not choices:
                self.notify("No configurable tools are available", severity="warning")
                return
            selected = await self.push_screen_wait(
                ChoiceScreen(
                    title="TOOL PERMISSIONS",
                    placeholder="Filter tools…",
                    choices=choices,
                )
            )
        if selected is None:
            return
        tool = next((item for item in tools if item.name == selected), None)
        if tool is None:
            await self.query_one(ConversationView).add_notice(
                f"Tool not found: {selected}",
                error=True,
            )
            return
        if tool.permission_control == "tool":
            self.notify(
                f"{tool.name} permission is fixed by its tool contract",
                severity="warning",
            )
            return
        current = tool.permission_override or "inherit"
        permission = await self.push_screen_wait(
            ChoiceScreen(
                title=f"TOOL PERMISSION · {tool.name}",
                placeholder="Filter permission modes…",
                choices=tuple(
                    Choice(
                        value,
                        ("● " if value == current else "  ") + value,
                        _tool_permission_description(value),
                    )
                    for value in _TOOL_PERMISSION_CHOICES
                ),
            )
        )
        if permission is None:
            return
        try:
            await self._controller.set_tool_permission(
                tool.name,
                _TOOL_PERMISSION_CHOICES[permission],
            )
        except Exception as exc:  # noqa: BLE001 - application validation is user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self.notify(f"Tool permission saved: {tool.name} → {permission} (next restart)")

    async def _configure_tool_execution(self) -> None:
        try:
            current = await self._controller.tool_execution_configuration()
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        draft = await self.push_screen_wait(ToolExecutionConfigurationScreen(current))
        if draft is None:
            return
        try:
            updated = await self._controller.update_tool_execution(draft)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        scope = "workspace only" if updated.workspace_only else "external paths allowed"
        self.notify(f"Tool execution saved: {updated.mode}, {scope} (next restart)")

    @work(exclusive=True, group="configuration")
    async def _choose_mcp(self, selector: str | None = None) -> None:
        try:
            configs = await self._controller.mcp_configurations()
            runtime = {item.server_id: item for item in self._controller.mcp_servers()}
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        selected = selector
        if selected is None:
            choices = tuple(
                Choice(
                    config.server_id,
                    config.name,
                    _mcp_description(config, runtime.get(config.server_id)),
                )
                for config in configs
            )
            if not choices:
                self.notify("No MCP servers are configured", severity="warning")
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

    async def _choose_mcp_action(self, action: str) -> None:
        if action not in {"connect", "disconnect", "edit", "remove"}:
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

        if action == "connect":
            candidates = tuple(
                config
                for config in configs
                if config.enabled
                and (
                    (status := runtime.get(config.server_id)) is not None
                    and status.status not in {"connected", "starting", "reconnecting"}
                )
            )
        elif action == "disconnect":
            candidates = tuple(
                config
                for config in configs
                if (status := runtime.get(config.server_id)) is not None
                and status.status in {"connected", "starting", "reconnecting"}
            )
        else:
            candidates = configs

        action_label = {
            "connect": "CONNECT",
            "disconnect": "DISCONNECT",
            "edit": "EDIT",
            "remove": "REMOVE",
        }[action]
        if not candidates:
            self.notify(f"No MCP servers are available to {action}", severity="warning")
            return
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=f"{action_label} MCP SERVER",
                placeholder="Filter servers…",
                choices=tuple(
                    Choice(
                        config.server_id,
                        config.name,
                        _mcp_description(config, runtime.get(config.server_id)),
                    )
                    for config in candidates
                ),
            )
        )
        if selected is None:
            return
        config = next(item for item in candidates if item.server_id == selected)
        if action == "edit":
            await self._edit_mcp(config)
        elif action == "remove":
            await self._remove_mcp(selected)
        else:
            await self._run_mcp_action(
                selected,
                "start" if action == "connect" else "stop",
            )

    async def _manage_mcp(self, server_id: str) -> None:
        try:
            config = await self._controller.mcp_configuration(server_id)
            status = next(
                (item for item in self._controller.mcp_servers() if item.server_id == server_id),
                None,
            )
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        actions = [
            Choice("edit", "Edit configuration", "Saved changes apply on the next restart"),
            Choice("check", "Check connection", "Open a temporary validated connection"),
        ]
        if status is not None:
            if status.status == "connected":
                actions.extend(
                    (
                        Choice("refresh", "Refresh tools", "Rediscover this server's tools"),
                        Choice("restart", "Restart runtime", "Restart the current connection"),
                        Choice("stop", "Stop runtime", "Stop the current connection"),
                    )
                )
            else:
                actions.append(Choice("start", "Start runtime", "Start the current connection"))
        actions.append(Choice("remove", "Remove configuration", "Delete after confirmation"))
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=f"MCP · {config.name}",
                placeholder="Choose an action…",
                choices=tuple(actions),
            )
        )
        if selected == "edit":
            await self._edit_mcp(config)
        elif selected == "check":
            await self._check_mcp(server_id)
        elif selected in {"start", "stop", "restart", "refresh"}:
            await self._run_mcp_action(server_id, selected)
        elif selected == "remove":
            await self._remove_mcp(server_id)

    async def _edit_mcp(self, existing: MCPServerConfigView | None) -> None:
        draft = await self.push_screen_wait(MCPConfigurationScreen(existing))
        if draft is None:
            return
        try:
            saved = await self._controller.save_mcp_configuration(draft)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self._refresh_mcp_system_status()
        self.notify(f"MCP configuration saved: {saved.server_id} (next restart)")

    async def _check_mcp(self, server_id: str) -> None:
        try:
            result = await self._controller.check_mcp_server(server_id)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        if result.ok:
            self.notify(f"MCP check passed: {server_id} · {len(result.tool_names)} tool(s)")
        else:
            await self.query_one(ConversationView).add_notice(
                f"MCP check failed: {server_id} · {result.error or result.status}",
                error=True,
            )

    async def _run_mcp_action(self, server_id: str, action: str) -> None:
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
            return
        self._refresh_mcp_system_status()
        self.notify(
            f"MCP {server_id}: {status.status} · {status.tool_count} tool(s)",
            severity="error" if status.status == "failed" else "information",
        )

    async def _remove_mcp(self, server_id: str) -> None:
        confirmed = await self.push_screen_wait(
            ConfirmScreen(
                title="REMOVE MCP SERVER",
                message=(
                    f"Remove {server_id} from configuration? The current runtime is unchanged "
                    "until restart."
                ),
                confirm_label="Remove",
            )
        )
        if not confirmed:
            return
        try:
            await self._controller.remove_mcp_configuration(server_id)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self._refresh_mcp_system_status()
        self.notify(f"MCP configuration removed: {server_id} (next restart)")

    @work(exclusive=True, group="configuration")
    async def _choose_skills(self, selector: str | None = None) -> None:
        try:
            catalog = await self._controller.skill_catalog()
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        selected = selector
        if selected is None:
            choices = tuple(
                Choice(skill.name, skill.name, _skill_description(skill))
                for skill in catalog.skills
            )
            if not choices:
                self.notify("No skills are installed or discovered", severity="warning")
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

    async def _choose_skill_action(self, action: str) -> None:
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
            qualifier = "managed " if action in {"update", "remove"} else ""
            self.notify(f"No {qualifier}skills are available to {action}", severity="warning")
            return
        action_label = {
            "toggle": "ENABLE OR DISABLE",
            "validate": "VALIDATE",
            "update": "UPDATE",
            "remove": "REMOVE",
        }[action]
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=f"{action_label} SKILL",
                placeholder="Filter skills…",
                choices=tuple(
                    Choice(skill.name, skill.name, _skill_description(skill))
                    for skill in candidates
                ),
            )
        )
        if selected is None:
            return
        skill = next(item for item in candidates if item.name == selected)
        if action == "toggle":
            await self._set_skill_enabled(skill.name, enabled=not skill.enabled)
        elif action == "validate":
            await self._validate_skill(skill.name)
        elif action == "update":
            await self._update_skill(skill.name)
        else:
            await self._remove_skill(skill.name)

    async def _manage_skill(self, name: str) -> None:
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
        actions = [
            Choice(
                "disable" if skill.enabled else "enable",
                "Disable" if skill.enabled else "Enable",
                "Apply from the next runtime",
            ),
            Choice("validate", "Validate", "Validate SKILL.md and referenced structure"),
        ]
        if skill.managed:
            actions.extend(
                (
                    Choice("update", "Update", "Refresh from the recorded source"),
                    Choice("remove", "Remove", "Delete this managed installation"),
                )
            )
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=f"SKILL · {name}",
                placeholder="Choose an action…",
                choices=tuple(actions),
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

    async def _install_skill(self) -> None:
        draft = await self.push_screen_wait(SkillInstallScreen())
        if draft is None:
            return
        try:
            result = await self._controller.install_skills(draft)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        names = ", ".join(item.name for item in result.skills) or "none"
        self.notify(f"Skills installed: {names} (next restart)")

    async def _set_skill_enabled(self, name: str, *, enabled: bool) -> None:
        try:
            await self._controller.set_skill_enabled(name, enabled=enabled)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self.notify(f"Skill {'enabled' if enabled else 'disabled'}: {name} (next restart)")

    async def _validate_skill(self, name: str) -> None:
        try:
            result = await self._controller.validate_skill(name)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        if result.valid:
            self.notify(f"Skill validation passed: {name}")
            return
        messages = "; ".join(item.message for item in result.diagnostics) or "invalid skill"
        await self.query_one(ConversationView).add_notice(
            f"Skill validation failed: {name} · {messages}",
            error=True,
        )

    async def _update_skill(self, name: str) -> None:
        try:
            await self._controller.update_skill(name)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self.notify(f"Skill updated: {name} (next restart)")

    async def _remove_skill(self, name: str) -> None:
        confirmed = await self.push_screen_wait(
            ConfirmScreen(
                title="REMOVE SKILL",
                message=f"Remove the managed skill {name}?",
                confirm_label="Remove",
            )
        )
        if not confirmed:
            return
        try:
            await self._controller.remove_skill(name)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self.notify(f"Skill removed: {name} (next restart)")

    def _refresh_mcp_system_status(self) -> None:
        runtime = self._controller.mcp_servers()
        self._system_status = project_mcp_system_status(self._system_status, runtime)
        self.query_one(ConversationView).set_system_status(self._system_status)


def _tool_description(tool: TuiToolConfiguration) -> str:
    permission = tool.effective_permission or "unavailable"
    override = (
        f"override: {tool.permission_override}"
        if tool.permission_override is not None
        else "definition default"
    )
    control = " · contract-controlled" if tool.permission_control == "tool" else ""
    availability = "" if tool.available else " · not loaded"
    return (
        f"{tool.source} · {tool.effect} · permission: {permission} ({override})"
        f"{control}{availability}"
    )


def _tool_permission_description(value: str) -> str:
    descriptions = {
        "inherit": "Clear the override and use the tool definition",
        "auto": "Execute without approval unless a stricter policy applies",
        "approval": "Require user or Host-policy approval",
        "deny": "Reject the tool before its handler starts",
    }
    return descriptions[value]


def _mcp_description(
    config: MCPServerConfigView,
    status: McpServerStatus | None,
) -> str:
    runtime = status.status if status is not None else "next restart"
    tools = f" · {status.tool_count} tool(s)" if status is not None else ""
    enabled = "enabled" if config.enabled else "disabled"
    return (
        f"{config.transport} · {enabled} · runtime: {runtime}{tools}"
        f" · permission: {config.permission_mode}"
    )


def _skill_description(skill: SkillView) -> str:
    status = "enabled" if skill.enabled else "disabled"
    managed = "managed" if skill.managed else "discovered"
    return f"{status} · {managed} · {skill.source} · {skill.description}"
