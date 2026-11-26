from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from typing import cast

from ouro.application.contracts.plugin_configuration import PluginConfigurationResult
from ouro.application.contracts.plugins import PluginDetail, PluginSummary
from ouro.tui.features.plugins.configuration import PluginConfigurationScreen
from ouro.tui.features.plugins.install import PluginInstallScreen
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.confirmation import ConfirmationScreen
from ouro.tui.menus.models import Choice
from ouro.tui.menus.workflows import WorkflowNavigation
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme
from ouro.tui.view_models import TuiPluginInstallConfiguration

_CONTRIBUTION_LABELS = {
    "tools": "tool",
    "commands": "command",
    "observers": "observer",
    "context_providers": "context",
    "system_prompt_providers": "system prompt",
    "compaction_strategies": "compaction",
    "skills": "skill",
    "mcp_servers": "mcp",
    "background_services": "service",
    "providers": "provider",
    "run_drivers": "driver",
    "continuation_policies": "continuation",
    "prompt_policies": "prompt",
    "turn_policies": "turn",
    "tool_policies": "tool policy",
    "session_controllers": "controller",
}


class PluginManagementWorkflows:
    """Project application-owned plugin management into shared TUI menus."""

    def __init__(self, navigation: WorkflowNavigation) -> None:
        self._ui = navigation

    async def _choose_plugins(
        self,
        *,
        menu: ChoiceScreen | None = None,
        switch: bool = False,
    ) -> None:
        revision = menu.page_revision if menu is not None else None
        try:
            with self._ui.loading(None):
                plugins = await self._ui.controller().plugins()
        except Exception as exc:  # noqa: BLE001 - application errors are user-visible.
            self._ui.notify_error(exc, title="Could not load plugins")
            return
        if not plugins:
            return
        if menu is not None and (not menu.is_attached or menu.page_revision != revision):
            return
        choices = _plugin_choices(plugins)
        plugin_menu: ChoiceScreen

        async def refresh_plugins() -> tuple[Choice, ...] | None:
            try:
                with self._ui.loading(None):
                    return _plugin_choices(await self._ui.controller().plugins())
            except Exception as exc:  # noqa: BLE001
                self._ui.notify_error(exc, title="Could not load plugins")
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
        self._ui.show(plugin_menu)

    async def _manage_plugin(
        self,
        plugin_id: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        try:
            plugin = await self._ui.controller().plugin(plugin_id)
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Could not load plugins")
            return

        async def select_action(selected: str) -> tuple[Choice, ...] | None:
            with self._ui.loading(selected) if selected in {"enable", "disable"} else nullcontext():
                if selected in {"enable", "disable"}:
                    await self._set_plugin_enabled(plugin_id, enabled=selected == "enable")
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
                    )
                    return None
                if menu is not None:
                    refreshed = await self._ui.controller().plugin(plugin_id)
                    return _plugin_action_choices(refreshed, theme=self._ui.theme())
                return None

        choices = _plugin_action_choices(plugin, theme=self._ui.theme())
        if menu is not None:

            async def refresh_plugin_actions() -> tuple[Choice, ...] | None:
                try:
                    refreshed = await self._ui.controller().plugin(plugin_id)
                    return _plugin_action_choices(refreshed, theme=self._ui.theme())
                except Exception as exc:  # noqa: BLE001
                    self._ui.notify_error(exc, title="Could not load plugins")
                    return None

            menu.push_page(
                title=self._ui.theme().inline_separator.join(("PLUGIN", plugin.name)),
                description=_plugin_description(plugin, theme=self._ui.theme()),
                placeholder=None,
                choices=choices,
                on_select=select_action,
                on_refresh=refresh_plugin_actions,
            )
            return
        menu = ChoiceScreen(
            title=self._ui.theme().inline_separator.join(("PLUGIN", plugin.name)),
            description=_plugin_description(plugin, theme=self._ui.theme()),
            placeholder=None,
            choices=choices,
            on_select=select_action,
        )
        await self._ui.choose(menu)

    async def _install_plugin(self, *, menu: ChoiceScreen | None = None) -> None:
        draft = await (
            menu.open_child(PluginInstallScreen())
            if menu is not None
            else self._ui.choose(PluginInstallScreen())
        )
        if draft is None:
            return
        try:
            with self._ui.loading(None):
                result = await self._ui.controller().install_plugin(
                    cast(TuiPluginInstallConfiguration, draft)
                )
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Could not install the plugin")
            return
        suffix = "installed" if result.changed else "already installed"
        self._ui.notify(
            self._ui.theme().inline_separator.join(
                (f"Plugin {suffix}: {result.plugin_id}", result.status.replace("_", " "))
            ),
            markup=False,
        )

    async def _configure_plugin(
        self,
        plugin_id: str,
        *,
        menu: ChoiceScreen | None,
    ) -> None:
        try:
            with self._ui.loading("configure"):
                view = await self._ui.controller().plugin_configuration(plugin_id)
            if not view.configuration_supported:
                self._ui.notify(
                    f"There are no settings to change for {plugin_id}.",
                    severity="warning",
                    markup=False,
                )
                return

            async def reload_configuration():
                return await self._ui.controller().plugin_configuration(
                    plugin_id, package_digest=view.package_digest
                )

            screen = PluginConfigurationScreen(
                view,
                preview=self._ui.controller().preview_plugin_configuration,
                apply=self._ui.controller().configure_plugin,
                reload=reload_configuration,
                theme=self._ui.theme(),
            )
            result = await (
                menu.open_child(screen) if menu is not None else self._ui.choose(screen)
            )
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Plugin settings")
            return
        if result is None:
            return
        applied = cast(PluginConfigurationResult, result)
        outcome = {
            "unchanged": "Settings already up to date",
            "saved": "Settings saved",
            "published": "Settings applied",
        }[applied.outcome]
        summary = f"{outcome} for {plugin_id}. Plugin status: {applied.status.replace('_', ' ')}."
        if applied.status in {"degraded", "failed"}:
            self._ui.notify(
                "\n".join((summary, *(item.message for item in applied.diagnostics))),
                severity="error" if applied.status == "failed" else "warning",
                markup=False,
            )
        else:
            self._ui.notify(summary, markup=False)

    async def _set_plugin_enabled(self, plugin_id: str, *, enabled: bool) -> bool:
        operation = (
            self._ui.controller().enable_plugin if enabled else self._ui.controller().disable_plugin
        )
        try:
            await operation(plugin_id)
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Could not change the plugin status")
            return False
        return True

    async def _restart_plugin(self, plugin_id: str) -> None:
        try:
            with self._ui.loading("restart"):
                await self._ui.controller().restart_plugin(plugin_id)
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Could not restart the plugin")
            return

    async def _choose_plugin_install(
        self,
        plugin_id: str,
        *,
        menu: ChoiceScreen | None,
    ) -> None:
        plugin = await self._ui.controller().plugin(plugin_id)
        selected_install = next(
            (install.package_digest for install in plugin.installs if install.selected), None
        )
        choices = tuple(
            Choice(
                install.package_digest,
                install.version,
                trailing=("selected" if install.selected else install.package_digest[:12]),
                marker=self._ui.theme().selected_choice_marker if install.selected else "",
            )
            for install in plugin.installs
        )

        async def select_install(package_digest: str) -> tuple[Choice, ...] | None:
            try:
                with self._ui.loading(package_digest):
                    result = await self._ui.controller().select_plugin_install(
                        plugin_id,
                        package_digest,
                    )
                    if menu is not None:
                        refreshed = await self._ui.controller().plugin(plugin_id)
                        menu.update_parent_choices(
                            _plugin_action_choices(refreshed, theme=self._ui.theme()),
                            description=_plugin_description(refreshed, theme=self._ui.theme()),
                        )
            except Exception as exc:  # noqa: BLE001
                self._ui.notify_error(exc, title="Plugin versions")
                return None
            return tuple(
                replace(
                    choice,
                    marker=(
                        self._ui.theme().selected_choice_marker
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
                title=self._ui.theme().inline_separator.join(("PLUGIN VERSIONS", plugin.name)),
                placeholder="Filter versions…",
                choices=choices,
                initial_value=selected_install,
                on_select=select_install,
            )
            return
        await self._ui.choose(
            ChoiceScreen(
                title=self._ui.theme().inline_separator.join(("PLUGIN VERSIONS", plugin.name)),
                placeholder="Filter versions…",
                choices=choices,
                initial_value=selected_install,
                on_select=select_install,
            )
        )

    async def _remove_plugin(
        self,
        plugin_id: str,
        *,
        menu: ChoiceScreen | None,
    ) -> None:
        revision = menu.page_revision if menu is not None else None
        plugin = await self._ui.controller().plugin(plugin_id)
        if menu is not None and (not menu.is_attached or menu.page_revision != revision):
            return
        selected = await (
            menu.open_child(
                ConfirmationScreen(
                    title="DELETE PLUGIN",
                    message=(
                        f"Delete the current version of {plugin.name}? "
                        "Your plugin data will be kept."
                    ),
                    confirm_label="Delete",
                )
            )
            if menu is not None
            else self._ui.choose(
                ConfirmationScreen(
                    title="DELETE PLUGIN",
                    message=(
                        f"Delete the current version of {plugin.name}? "
                        "Your plugin data will be kept."
                    ),
                    confirm_label="Delete",
                )
            )
        )
        if not ConfirmationScreen.confirmed(selected):
            return
        try:
            with self._ui.loading("remove"):
                await self._ui.controller().remove_plugin(plugin_id)
        except Exception as exc:  # noqa: BLE001
            self._ui.notify_error(exc, title="Could not delete the plugin")
            return
        if menu is not None:
            with self._ui.loading(None):
                choices = _plugin_choices(await self._ui.controller().plugins())
            if not menu.is_attached or menu.page_revision != revision:
                return
            if not menu.pop_page(
                choices=choices,
                initial_value=plugin_id,
            ):
                menu.dismiss(None)


def _plugin_choices(plugins: tuple[PluginSummary, ...]) -> tuple[Choice, ...]:
    return tuple(
        Choice(
            plugin.plugin_id,
            plugin.name,
            description=plugin.plugin_id,
            enabled_state=plugin.desired_enabled,
            group="Built-in" if plugin.plugin_class == "builtin" else "Installed",
            trailing=f"v{plugin.version}",
        )
        for plugin in sorted(
            plugins,
            key=lambda item: (item.plugin_class != "builtin", item.name.casefold()),
        )
    )


def _plugin_description(plugin: PluginDetail, *, theme: TuiTheme = DEFAULT_TUI_THEME) -> str:
    metadata = f"v{plugin.version}"
    if plugin.author is not None:
        metadata += theme.inline_separator + f"By {plugin.author.name}"
    return f"{plugin.description}\n\n{metadata}" if plugin.description else metadata


def _plugin_action_choices(
    plugin: PluginDetail,
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[Choice, ...]:
    actions: list[Choice] = []
    if plugin.plugin_class == "user":
        if plugin.configuration_supported:
            actions.append(Choice("configure", "Settings", group="ACTIONS"))
        if len(plugin.installs) > 1:
            actions.append(
                Choice(
                    "select-install",
                    "Switch version",
                    trailing=f"{len(plugin.installs)} versions",
                    group="ACTIONS",
                )
            )
    actions.extend(
        (
            Choice(
                "disable" if plugin.desired_enabled else "enable",
                "Disable" if plugin.desired_enabled else "Enable",
                trailing="" if plugin.status == "disabled" else plugin.status,
                group="ACTIONS",
            ),
            Choice(
                "restart",
                "Restart",
                group="ACTIONS",
                disabled=plugin.status not in {"active", "degraded", "failed"},
            ),
        )
    )
    if plugin.plugin_class == "user":
        actions.append(
            Choice("remove", "Delete", group="ACTIONS", accent=theme.danger, destructive=True)
        )
    contributions = tuple(
        Choice(
            f"contribution:{kind}:{name}",
            f"/{name}" if kind == "commands" else name,
            group="CONTRIBUTIONS",
            trailing=_CONTRIBUTION_LABELS.get(kind, kind.replace("_", " ")),
            disabled=True,
        )
        for kind, names in plugin.contributions.items()
        for name in names
    )
    return (*contributions, *actions)
