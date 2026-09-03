from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import cast

from ouro.application.plugin_models import PluginDetail, PluginSummary
from ouro.tui.configuration_screens import PluginConfigurationScreen, PluginInstallScreen
from ouro.tui.controller import (
    TuiController,
    TuiPluginInstallConfiguration,
    TuiSystemStatus,
    project_plugin_system_status,
)
from ouro.tui.provider_workflows import ProviderConfigurationWorkflows
from ouro.tui.screens import Choice, ChoiceScreen, ConfirmationScreen
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, theme_for
from ouro.tui.widgets import ConversationView


class PluginManagementWorkflows(ProviderConfigurationWorkflows):
    """Project application-owned plugin management into shared TUI menus."""

    _controller: TuiController
    _system_status: TuiSystemStatus
    _refresh_session_controls: Callable[[], Awaitable[None]]

    async def _choose_plugins(
        self,
        *,
        menu: ChoiceScreen | None = None,
        switch: bool = False,
    ) -> None:
        try:
            plugins = self._controller.plugins()
        except Exception as exc:  # noqa: BLE001 - application errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        if not plugins:
            return
        choices = _plugin_choices(plugins, theme=theme_for(self))
        plugin_menu: ChoiceScreen

        async def refresh_plugins() -> tuple[Choice, ...] | None:
            try:
                return _plugin_choices(
                    self._controller.plugins(), theme=theme_for(self)
                )
            except Exception as exc:  # noqa: BLE001
                await self.query_one(ConversationView).add_notice(str(exc), error=True)
                return None

        async def select_plugin(plugin_id: str) -> tuple[Choice, ...] | None:
            await self._manage_plugin(plugin_id, menu=plugin_menu)
            return None

        if menu is not None:
            plugin_menu = menu
            navigate = menu.switch_page if switch else menu.push_page
            navigate(
                navigation_id="plugins",
                title="PLUGINS",
                placeholder="Filter plugins…",
                choices=choices,
                on_select=select_plugin,
                on_refresh=refresh_plugins,
            )
            return
        plugin_menu = ChoiceScreen(
            navigation_id="plugins",
            title="PLUGINS",
            placeholder="Filter plugins…",
            choices=choices,
            on_select=select_plugin,
            on_refresh=refresh_plugins,
        )
        self.push_screen(plugin_menu)

    async def _choose_plugin_action(
        self,
        action: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        if action not in {"toggle", "check", "remove"}:
            await self.query_one(ConversationView).add_notice(
                f"Unknown plugin action: {action}",
                error=True,
            )
            return
        try:
            plugins = self._controller.plugins()
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        candidates = tuple(
            plugin for plugin in plugins if action != "remove" or plugin.plugin_class == "user"
        )
        if not candidates:
            return
        plugin_catalog = {plugin.plugin_id: plugin for plugin in candidates}

        async def refresh_candidates() -> tuple[Choice, ...] | None:
            try:
                refreshed = self._controller.plugins()
            except Exception as exc:  # noqa: BLE001
                await self.query_one(ConversationView).add_notice(str(exc), error=True)
                return None
            refreshed_candidates = tuple(
                plugin
                for plugin in refreshed
                if action != "remove" or plugin.plugin_class == "user"
            )
            plugin_catalog.clear()
            plugin_catalog.update(
                {plugin.plugin_id: plugin for plugin in refreshed_candidates}
            )
            return _plugin_choices(refreshed_candidates, theme=theme_for(self))

        async def select_plugin(plugin_id: str) -> tuple[Choice, ...] | None:
            plugin = plugin_catalog[plugin_id]
            if action == "toggle":
                if not await self._set_plugin_enabled(
                    plugin_id,
                    enabled=not plugin.desired_enabled,
                ):
                    return None
                refreshed = self._controller.plugins()
                plugin_catalog.clear()
                plugin_catalog.update({item.plugin_id: item for item in refreshed})
                return _plugin_choices(refreshed, theme=theme_for(self))
            if action == "check":
                await self._check_plugin(plugin_id)
            else:
                await self._remove_plugin(
                    plugin_id,
                    menu=menu,
                    return_to_catalog=False,
                )
            return None

        title = {
            "toggle": "ENABLE OR DISABLE PLUGIN",
            "check": "CHECK PLUGIN",
            "remove": "REMOVE PLUGIN",
        }[action]
        choices = _plugin_choices(candidates, theme=theme_for(self))
        if menu is not None:
            menu.push_page(
                title=title,
                placeholder="Filter plugins…",
                choices=choices,
                on_select=select_plugin,
                on_refresh=refresh_candidates,
                retain_focus_position_on_select=action == "toggle",
            )
            return
        await self.push_screen_wait(
            ChoiceScreen(
                title=title,
                placeholder="Filter plugins…",
                choices=choices,
                on_select=select_plugin,
                on_refresh=refresh_candidates,
                retain_focus_position_on_select=action == "toggle",
            )
        )

    async def _manage_plugin(
        self,
        plugin_id: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        try:
            plugin = self._controller.plugin(plugin_id)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return

        def cancel_plugin() -> bool:
            if menu is None:
                return False
            menu.pop_page(
                choices=_plugin_choices(
                    self._controller.plugins(), theme=theme_for(self)
                ),
                initial_value=plugin_id,
            )
            return True

        async def select_action(selected: str) -> tuple[Choice, ...] | None:
            if selected in {"enable", "disable"}:
                await self._set_plugin_enabled(plugin_id, enabled=selected == "enable")
            elif selected == "check":
                await self._check_plugin(plugin_id)
            elif selected == "restart":
                await self._restart_plugin(plugin_id)
            elif selected == "configure":
                await self._configure_plugin(plugin_id, menu=menu)
            elif selected == "select-install":
                await self._choose_plugin_install(plugin_id, menu=menu)
                return None
            elif selected == "remove":
                await self._remove_plugin(
                    plugin_id,
                    menu=menu,
                    return_to_catalog=True,
                )
                return None
            if menu is not None:
                refreshed = self._controller.plugin(plugin_id)
                menu.update_parent_choices(
                    _plugin_choices(
                        self._controller.plugins(), theme=theme_for(self)
                    ),
                    initial_value=plugin_id,
                )
                return _plugin_action_choices(refreshed)
            return None

        choices = _plugin_action_choices(plugin)
        if menu is not None:
            async def refresh_plugin_actions() -> tuple[Choice, ...] | None:
                try:
                    return _plugin_action_choices(self._controller.plugin(plugin_id))
                except Exception as exc:  # noqa: BLE001
                    await self.query_one(ConversationView).add_notice(str(exc), error=True)
                    return None

            menu.push_page(
                title=theme_for(self).inline_separator.join(("PLUGIN", plugin.name)),
                placeholder="Choose an action…",
                choices=choices,
                on_select=select_action,
                on_refresh=refresh_plugin_actions,
                on_cancel=cancel_plugin,
            )
            return
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=theme_for(self).inline_separator.join(("PLUGIN", plugin.name)),
                placeholder="Choose an action…",
                choices=choices,
            )
        )
        if selected is not None:
            await select_action(selected)

    async def _install_plugin(self, *, menu: ChoiceScreen | None = None) -> None:
        draft = await (
            menu.open_child(PluginInstallScreen())
            if menu is not None
            else self.push_screen_wait(PluginInstallScreen())
        )
        if draft is None:
            return
        try:
            result = await self._controller.install_plugin(
                cast(TuiPluginInstallConfiguration, draft)
            )
        except Exception as exc:  # noqa: BLE001
            self.notify(str(exc), severity="error", markup=False)
            return
        self._refresh_plugin_system_status()
        suffix = "installed" if result.changed else "already installed"
        self.notify(
            theme_for(self).inline_separator.join(
                (f"Plugin {suffix}: {result.plugin_id}", result.status)
            )
        )

    async def _configure_plugin(
        self,
        plugin_id: str,
        *,
        menu: ChoiceScreen | None,
    ) -> None:
        plugin = self._controller.plugin(plugin_id)
        if not plugin.configuration_supported:
            self.notify(f"Plugin has no configuration schema: {plugin_id}", severity="warning")
            return
        draft = await (
            menu.open_child(
                PluginConfigurationScreen(plugin, theme=theme_for(self))
            )
            if menu is not None
            else self.push_screen_wait(
                PluginConfigurationScreen(plugin, theme=theme_for(self))
            )
        )
        if draft is None:
            return
        values, credential_refs = cast(tuple[dict[str, object], dict[str, str]], draft)
        try:
            await self._controller.configure_plugin(
                plugin_id,
                values,
                credential_refs,
            )
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self._refresh_plugin_system_status()

    async def _set_plugin_enabled(self, plugin_id: str, *, enabled: bool) -> bool:
        operation = self._controller.enable_plugin if enabled else self._controller.disable_plugin
        try:
            await operation(plugin_id)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return False
        self._refresh_plugin_system_status()
        await self._refresh_session_controls()
        return True

    async def _check_plugin(self, plugin_id: str) -> None:
        try:
            result = await self._controller.check_plugin(plugin_id)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        messages = "; ".join(item.message for item in result.diagnostics)
        if result.status in {"active", "ready", "disabled"} and not messages:
            self.notify(
                theme_for(self).inline_separator.join(
                    (f"Plugin check passed: {plugin_id}", result.status)
                )
            )
            return
        await self.query_one(ConversationView).add_notice(
            theme_for(self).inline_separator.join(
                (
                    f"Plugin check: {plugin_id}",
                    result.status,
                    *((messages,) if messages else ()),
                )
            ),
            error=result.status in {"invalid", "incompatible", "blocked", "failed"},
        )

    async def _restart_plugin(self, plugin_id: str) -> None:
        try:
            await self._controller.restart_plugin(plugin_id)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self._refresh_plugin_system_status()
        await self._refresh_session_controls()

    async def _choose_plugin_install(
        self,
        plugin_id: str,
        *,
        menu: ChoiceScreen | None,
    ) -> None:
        plugin = self._controller.plugin(plugin_id)
        choices = tuple(
            Choice(
                install.package_digest,
                install.version,
                trailing=("selected" if install.selected else install.package_digest[:12]),
                marker=theme_for(self).selected_choice_marker if install.selected else "",
            )
            for install in plugin.installs
        )

        async def select_install(package_digest: str) -> tuple[Choice, ...] | None:
            try:
                result = await self._controller.select_plugin_install(
                    plugin_id,
                    package_digest,
                )
            except Exception as exc:  # noqa: BLE001
                await self.query_one(ConversationView).add_notice(str(exc), error=True)
                return None
            self._refresh_plugin_system_status()
            await self._refresh_session_controls()
            return tuple(
                replace(
                    choice,
                    marker=(
                        theme_for(self).selected_choice_marker
                        if choice.value == result.package_digest
                        else ""
                    ),
                    trailing=(
                        "selected" if choice.value == result.package_digest else choice.value[:12]
                    ),
                )
                for choice in choices
            )

        if menu is not None:
            menu.push_page(
                title=theme_for(self).inline_separator.join(
                    ("PLUGIN VERSIONS", plugin.name)
                ),
                placeholder="Filter versions…",
                choices=choices,
                on_select=select_install,
            )
            return
        await self.push_screen_wait(
            ChoiceScreen(
                title=theme_for(self).inline_separator.join(
                    ("PLUGIN VERSIONS", plugin.name)
                ),
                placeholder="Filter versions…",
                choices=choices,
                on_select=select_install,
            )
        )

    async def _remove_plugin(
        self,
        plugin_id: str,
        *,
        menu: ChoiceScreen | None,
        return_to_catalog: bool,
    ) -> None:
        plugin = self._controller.plugin(plugin_id)
        selected = await (
            menu.open_child(
                ConfirmationScreen(
                    title="REMOVE PLUGIN",
                    message=(
                        f"Remove the selected install of {plugin.name} ({plugin_id})? "
                        "Plugin data is retained."
                    ),
                    confirm_label="Remove",
                )
            )
            if menu is not None
            else self.push_screen_wait(
                ConfirmationScreen(
                    title="REMOVE PLUGIN",
                    message=f"Remove the selected install of {plugin.name} ({plugin_id})?",
                    confirm_label="Remove",
                )
            )
        )
        if not ConfirmationScreen.confirmed(selected):
            return
        try:
            await self._controller.remove_plugin(plugin_id)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self._refresh_plugin_system_status()
        await self._refresh_session_controls()
        if menu is not None:
            if return_to_catalog:
                menu.pop_page(
                    choices=_plugin_choices(
                        self._controller.plugins(), theme=theme_for(self)
                    ),
                    initial_value=plugin_id,
                )
            else:
                menu.pop_page()

    def _refresh_plugin_system_status(self) -> None:
        skill_names = self._controller.runtime_skill_names()
        self._system_status = project_plugin_system_status(
            replace(
                self._system_status,
                skill_count=len(skill_names),
                skill_names=skill_names,
            ),
            self._controller.plugins(),
        )
        self.query_one(ConversationView).set_system_status(self._system_status)


def _plugin_choices(
    plugins: tuple[PluginSummary, ...],
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[Choice, ...]:
    return tuple(
        Choice(
            plugin.plugin_id,
            plugin.name,
            description=plugin.plugin_id,
            group="Built-in" if plugin.plugin_class == "builtin" else "Installed",
            trailing=theme.inline_separator.join((f"v{plugin.version}", plugin.status)),
        )
        for plugin in sorted(
            plugins,
            key=lambda item: (item.plugin_class != "builtin", item.name.casefold()),
        )
    )


def _plugin_action_choices(plugin: PluginDetail) -> tuple[Choice, ...]:
    actions = [
        Choice(
            "disable" if plugin.desired_enabled else "enable",
            "Disable" if plugin.desired_enabled else "Enable",
            trailing=plugin.status,
            group="LIFECYCLE",
        ),
        Choice("check", "Check integrity and readiness", group="LIFECYCLE"),
        Choice(
            "restart",
            "Restart",
            group="LIFECYCLE",
            disabled=plugin.status not in {"active", "degraded", "failed"},
        ),
    ]
    if plugin.plugin_class == "user":
        if plugin.configuration_supported:
            actions.append(Choice("configure", "Configuration", group="SECURITY & CONFIG"))
        if len(plugin.installs) > 1:
            actions.append(
                Choice(
                    "select-install",
                    "Select installed version",
                    trailing=f"{len(plugin.installs)} versions",
                    group="VERSIONS",
                )
            )
        actions.append(Choice("remove", "Remove selected install", group="DANGER ZONE"))
    return tuple(actions)
