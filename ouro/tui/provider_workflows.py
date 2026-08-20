from __future__ import annotations

from typing import cast

from textual.app import App

from ouro.application.configuration_models import ProviderEndpointView, ProviderView
from ouro.tui.controller import (
    TuiController,
    TuiEndpointConfiguration,
    TuiProviderConfiguration,
)
from ouro.tui.provider_screens import (
    COMPATIBLE_PROVIDER_PROFILES,
    EndpointConfigurationScreen,
    ProviderConfigurationScreen,
    ProviderSetupProfile,
    profile_for_provider,
)
from ouro.tui.screens import Choice, ChoiceScreen, ConfirmationScreen
from ouro.tui.theme import SELECTED_CHOICE_MARKER
from ouro.tui.widgets import ConversationView


class ProviderConfigurationWorkflows(App[int]):
    """Own keyboard-first workflows for providers and their endpoints."""

    _controller: TuiController

    async def _choose_providers(self, *, menu: ChoiceScreen | None = None) -> None:
        try:
            catalog = await self._controller.provider_catalog()
        except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        if not catalog.providers:
            return
        choices = tuple(_provider_choice(provider) for provider in catalog.providers)
        if menu is not None:
            async def select_provider(provider_id: str) -> tuple[Choice, ...] | None:
                refreshed = await self._controller.provider_catalog()
                provider = next(
                    item for item in refreshed.providers if item.provider_id == provider_id
                )
                await self._manage_provider(provider, menu=menu)
                return None

            menu.push_page(
                title="PROVIDERS",
                placeholder="Filter providers…",
                choices=choices,
                on_select=select_provider,
            )
            return
        selected = await self.push_screen_wait(
            ChoiceScreen(title="PROVIDERS", placeholder="Filter providers…", choices=choices)
        )
        if selected is None:
            return
        provider = next(item for item in catalog.providers if item.provider_id == selected)
        await self._manage_provider(provider)

    async def _add_provider(self, *, menu: ChoiceScreen | None = None) -> None:
        try:
            templates = await self._controller.provider_templates()
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        profiles = {
            f"preset:{template.template_id}": ProviderSetupProfile.from_template(template)
            for template in templates
        }
        profiles.update(
            {
                f"compatible:{profile.provider_type}": profile
                for profile in COMPATIBLE_PROVIDER_PROFILES
            }
        )
        choices = (
            *(
                Choice(
                    f"preset:{template.template_id}",
                    template.display_name,
                    trailing=f"{template.endpoint_count} endpoints",
                    group="OURO presets",
                )
                for template in templates
            ),
            *(
                Choice(
                    f"compatible:{profile.provider_type}",
                    profile.display_name,
                    trailing=profile.provider_type,
                    group="Compatible",
                )
                for profile in COMPATIBLE_PROVIDER_PROFILES
            ),
        )
        if menu is not None:

            async def select_profile(selected: str) -> tuple[Choice, ...] | None:
                await self._create_provider(profiles[selected], menu=menu)
                return None

            menu.push_page(
                title="ADD PROVIDER",
                placeholder="Choose a provider type…",
                choices=choices,
                on_select=select_profile,
            )
            return
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title="ADD PROVIDER",
                placeholder="Choose a provider type…",
                choices=choices,
            )
        )
        if selected is None:
            return
        await self._create_provider(profiles[selected])

    async def _create_provider(
        self,
        profile: ProviderSetupProfile,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        screen = ProviderConfigurationScreen(profile)
        draft = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if draft is None:
            return
        provider_draft = cast(TuiProviderConfiguration, draft)
        try:
            provider = await self._controller.add_provider(provider_draft)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        suffix = (
            f"{provider.endpoint_count} preset endpoint(s)"
            if provider.endpoint_count
            else "add an endpoint to make it usable"
        )
        self.notify(f"Provider added: {provider.provider_id} · {suffix}")

    async def _choose_provider_action(
        self,
        action: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        if action not in {"edit", "toggle", "remove"}:
            await self.query_one(ConversationView).add_notice(
                f"Unknown provider action: {action}",
                error=True,
            )
            return
        try:
            catalog = await self._controller.provider_catalog()
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        candidates = tuple(catalog.providers)
        if not candidates:
            return
        choices = tuple(
            _provider_choice(
                provider,
                disabled=action == "toggle" and provider.default,
            )
            for provider in candidates
        )
        if action == "toggle":
            providers = {provider.provider_id: provider for provider in candidates}

            async def toggle_provider(provider_id: str) -> tuple[Choice, ...] | None:
                provider = providers[provider_id]
                saved = await self._set_provider_enabled(
                    provider,
                    enabled=not provider.enabled,
                )
                if saved is None:
                    return None
                try:
                    refreshed = await self._controller.provider_catalog()
                except Exception as exc:  # noqa: BLE001
                    await self.query_one(ConversationView).add_notice(
                        str(exc),
                        error=True,
                    )
                    return None
                providers.clear()
                providers.update(
                    {provider.provider_id: provider for provider in refreshed.providers}
                )
                return tuple(
                    _provider_choice(provider, disabled=provider.default)
                    for provider in providers.values()
                )

            title = f"{_provider_action_label(action)} PROVIDER"
            placeholder = "Filter providers…"
            if menu is not None:
                menu.push_page(
                    title=title,
                    placeholder=placeholder,
                    choices=choices,
                    on_select=toggle_provider,
                )
            else:
                await self.push_screen_wait(
                    ChoiceScreen(
                        title=title,
                        placeholder=placeholder,
                        choices=choices,
                        on_select=toggle_provider,
                    )
                )
            return
        if menu is not None:
            providers = {provider.provider_id: provider for provider in candidates}

            async def select_provider(provider_id: str) -> tuple[Choice, ...] | None:
                provider = providers[provider_id]
                if action == "edit":
                    await self._edit_provider(provider, menu=menu)
                else:
                    await self._remove_provider(provider.provider_id, menu=menu)
                return None

            menu.push_page(
                title=f"{_provider_action_label(action)} PROVIDER",
                placeholder="Filter providers…",
                choices=choices,
                on_select=select_provider,
            )
            return
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=f"{_provider_action_label(action)} PROVIDER",
                placeholder="Filter providers…",
                choices=choices,
            )
        )
        if selected is None:
            return
        provider = next(item for item in candidates if item.provider_id == selected)
        if action == "edit":
            await self._edit_provider(provider)
        else:
            await self._remove_provider(provider.provider_id)

    async def _manage_provider(
        self,
        provider: ProviderView,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        choices = _provider_management_choices(provider)
        if menu is not None:

            async def select_action(selected: str) -> tuple[Choice, ...] | None:
                nonlocal provider
                if selected == "edit":
                    await self._edit_provider(provider, menu=menu)
                elif selected == "toggle":
                    saved = await self._set_provider_enabled(
                        provider,
                        enabled=not provider.enabled,
                    )
                    if saved is None:
                        return None
                    provider = saved
                    catalog = await self._controller.provider_catalog()
                    menu.update_parent_choices(
                        tuple(_provider_choice(item) for item in catalog.providers),
                        initial_value=provider.provider_id,
                    )
                    return _provider_management_choices(provider)
                elif selected == "endpoints":
                    await self._choose_endpoints(provider, menu=menu)
                elif selected == "add-endpoint":
                    await self._edit_endpoint(provider, None, menu=menu)
                elif selected == "remove":
                    await self._remove_provider(provider.provider_id, menu=menu)
                return None

            menu.push_page(
                title=f"PROVIDER · {provider.provider_id}",
                placeholder="Choose an action…",
                choices=choices,
                on_select=select_action,
            )
            return
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=f"PROVIDER · {provider.provider_id}",
                placeholder="Choose an action…",
                choices=choices,
            )
        )
        if selected == "edit":
            await self._edit_provider(provider)
        elif selected == "toggle":
            await self._set_provider_enabled(provider, enabled=not provider.enabled)
        elif selected == "endpoints":
            await self._choose_endpoints(provider)
        elif selected == "add-endpoint":
            await self._edit_endpoint(provider, None)
        elif selected == "remove":
            await self._remove_provider(provider.provider_id)

    async def _edit_provider(
        self,
        provider: ProviderView,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        try:
            templates = await self._controller.provider_templates()
            profile = profile_for_provider(provider, templates)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        screen = ProviderConfigurationScreen(profile, existing=provider)
        draft = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if draft is None:
            return
        provider_draft = cast(TuiProviderConfiguration, draft)
        try:
            saved = await self._controller.update_provider(provider_draft)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self.notify(f"Provider saved: {saved.provider_id} (next request)")

    async def _set_provider_enabled(
        self,
        provider: ProviderView,
        *,
        enabled: bool,
    ) -> ProviderView | None:
        try:
            saved = await self._controller.set_provider_enabled(
                provider.provider_id,
                enabled=enabled,
            )
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return None
        return saved

    async def _remove_provider(
        self,
        provider_id: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        screen = ConfirmationScreen(
            title="REMOVE PROVIDER",
            message=(f"Remove {provider_id} and all of its endpoints from configuration?"),
            confirm_label="Remove",
        )
        selected = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if not ConfirmationScreen.confirmed(selected):
            return
        try:
            await self._controller.remove_provider(provider_id)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self.notify(f"Provider removed: {provider_id} (next request)")

    async def _choose_endpoint_action(
        self,
        action: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        if action not in {"list", "add", "edit", "use", "remove"}:
            await self.query_one(ConversationView).add_notice(
                f"Unknown endpoint action: {action}",
                error=True,
            )
            return
        try:
            catalog = await self._controller.provider_catalog()
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        if action == "add":
            providers = tuple(catalog.providers)
            if not providers:
                return
            if menu is not None:
                providers_by_id = {provider.provider_id: provider for provider in providers}

                async def select_provider(provider_id: str) -> tuple[Choice, ...] | None:
                    await self._edit_endpoint(
                        providers_by_id[provider_id],
                        None,
                        menu=menu,
                    )
                    return None

                menu.push_page(
                    title="ADD ENDPOINT · PROVIDER",
                    placeholder="Choose a provider…",
                    choices=tuple(_provider_choice(provider) for provider in providers),
                    on_select=select_provider,
                )
                return
            provider_id = await self.push_screen_wait(
                ChoiceScreen(
                    title="ADD ENDPOINT · PROVIDER",
                    placeholder="Choose a provider…",
                    choices=tuple(_provider_choice(provider) for provider in providers),
                )
            )
            if provider_id is None:
                return
            provider = next(item for item in providers if item.provider_id == provider_id)
            await self._edit_endpoint(provider, None)
            return

        routes = {
            f"{provider.provider_id}/{endpoint.endpoint_id}": (provider, endpoint)
            for provider in catalog.providers
            for endpoint in provider.endpoints
        }
        if not routes:
            return
        choices = tuple(
            _endpoint_route_choice(provider, endpoint) for provider, endpoint in routes.values()
        )
        if action == "use":

            async def set_default_route(route: str) -> tuple[Choice, ...] | None:
                provider, endpoint = routes[route]
                if not await self._use_endpoint(provider.provider_id, endpoint.endpoint_id):
                    return None
                try:
                    refreshed = await self._controller.provider_catalog()
                except Exception as exc:  # noqa: BLE001
                    await self.query_one(ConversationView).add_notice(
                        str(exc),
                        error=True,
                    )
                    return None
                routes.clear()
                routes.update(
                    {
                        f"{item.provider_id}/{candidate.endpoint_id}": (item, candidate)
                        for item in refreshed.providers
                        for candidate in item.endpoints
                    }
                )
                return tuple(
                    _endpoint_route_choice(item, candidate) for item, candidate in routes.values()
                )

            if menu is not None:
                menu.push_page(
                    title=_endpoint_action_label(action),
                    placeholder="Filter endpoints…",
                    choices=choices,
                    on_select=set_default_route,
                )
            else:
                await self.push_screen_wait(
                    ChoiceScreen(
                        title=_endpoint_action_label(action),
                        placeholder="Filter endpoints…",
                        choices=choices,
                        on_select=set_default_route,
                    )
                )
            return

        if menu is not None:

            async def select_route(route: str) -> tuple[Choice, ...] | None:
                provider, endpoint = routes[route]
                if action == "list":
                    await self._manage_endpoint(provider, endpoint, menu=menu)
                elif action == "edit":
                    await self._edit_endpoint(provider, endpoint, menu=menu)
                else:
                    await self._remove_endpoint(
                        provider.provider_id,
                        endpoint.endpoint_id,
                        menu=menu,
                    )
                return None

            menu.push_page(
                title=_endpoint_action_label(action),
                placeholder="Filter endpoints…",
                choices=choices,
                on_select=select_route,
            )
            return

        route = await self.push_screen_wait(
            ChoiceScreen(
                title=_endpoint_action_label(action),
                placeholder="Filter endpoints…",
                choices=choices,
            )
        )
        if route is None:
            return
        provider, endpoint = routes[route]
        if action == "list":
            await self._manage_endpoint(provider, endpoint)
        elif action == "edit":
            await self._edit_endpoint(provider, endpoint)
        else:
            await self._remove_endpoint(provider.provider_id, endpoint.endpoint_id)

    async def _choose_endpoints(
        self,
        provider: ProviderView,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        await self._choose_endpoint(provider, "manage", menu=menu)

    async def _choose_endpoint(
        self,
        provider: ProviderView,
        action: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        try:
            catalog = await self._controller.provider_endpoints(provider.provider_id)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        candidates = tuple(
            endpoint for endpoint in catalog.endpoints if action != "use" or not endpoint.default
        )
        if not candidates:
            return
        choices = tuple(
            _endpoint_choice(
                endpoint,
                available=provider.enabled,
                default=provider.default and endpoint.default,
            )
            for endpoint in candidates
        )
        if menu is not None:
            endpoints = {endpoint.endpoint_id: endpoint for endpoint in candidates}

            async def select_endpoint(endpoint_id: str) -> tuple[Choice, ...] | None:
                endpoint = endpoints[endpoint_id]
                if action == "manage":
                    await self._manage_endpoint(provider, endpoint, menu=menu)
                elif action == "edit":
                    await self._edit_endpoint(provider, endpoint, menu=menu)
                elif action == "use":
                    await self._use_endpoint(provider.provider_id, endpoint.endpoint_id)
                elif action == "remove":
                    await self._remove_endpoint(
                        provider.provider_id,
                        endpoint.endpoint_id,
                        menu=menu,
                    )
                return None

            menu.push_page(
                title=f"{_endpoint_action_label(action)} · {provider.provider_id}",
                placeholder="Filter endpoints…",
                choices=choices,
                on_select=select_endpoint,
            )
            return
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=f"{_endpoint_action_label(action)} · {provider.provider_id}",
                placeholder="Filter endpoints…",
                choices=choices,
            )
        )
        if selected is None:
            return
        endpoint = next(item for item in candidates if item.endpoint_id == selected)
        if action == "manage":
            await self._manage_endpoint(provider, endpoint)
        elif action == "edit":
            await self._edit_endpoint(provider, endpoint)
        elif action == "use":
            await self._use_endpoint(provider.provider_id, endpoint.endpoint_id)
        elif action == "remove":
            await self._remove_endpoint(provider.provider_id, endpoint.endpoint_id)

    async def _manage_endpoint(
        self,
        provider: ProviderView,
        endpoint: ProviderEndpointView,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        choices = [Choice("edit", "Edit endpoint")]
        if not endpoint.default:
            choices.append(Choice("use", "Set as default endpoint"))
        choices.append(Choice("remove", "Remove endpoint"))
        if menu is not None:

            async def select_action(selected: str) -> tuple[Choice, ...] | None:
                if selected == "edit":
                    await self._edit_endpoint(provider, endpoint, menu=menu)
                elif selected == "use":
                    await self._use_endpoint(provider.provider_id, endpoint.endpoint_id)
                elif selected == "remove":
                    await self._remove_endpoint(
                        provider.provider_id,
                        endpoint.endpoint_id,
                        menu=menu,
                    )
                return None

            menu.push_page(
                title=f"ENDPOINT · {provider.provider_id}/{endpoint.endpoint_id}",
                placeholder="Choose an action…",
                choices=tuple(choices),
                on_select=select_action,
            )
            return
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=f"ENDPOINT · {provider.provider_id}/{endpoint.endpoint_id}",
                placeholder="Choose an action…",
                choices=tuple(choices),
            )
        )
        if selected == "edit":
            await self._edit_endpoint(provider, endpoint)
        elif selected == "use":
            await self._use_endpoint(provider.provider_id, endpoint.endpoint_id)
        elif selected == "remove":
            await self._remove_endpoint(provider.provider_id, endpoint.endpoint_id)

    async def _edit_endpoint(
        self,
        provider: ProviderView,
        existing: ProviderEndpointView | None,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        screen = EndpointConfigurationScreen(provider, existing=existing)
        draft = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if draft is None:
            return
        endpoint_draft = cast(TuiEndpointConfiguration, draft)
        try:
            saved = (
                await self._controller.add_endpoint(endpoint_draft)
                if existing is None
                else await self._controller.update_endpoint(endpoint_draft)
            )
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        action = "added" if existing is None else "saved"
        self.notify(f"Endpoint {action}: {provider.provider_id}/{saved.endpoint_id} (next request)")

    async def _use_endpoint(self, provider_id: str, endpoint_id: str) -> bool:
        try:
            await self._controller.use_endpoint(provider_id, endpoint_id)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return False
        return True

    async def _remove_endpoint(
        self,
        provider_id: str,
        endpoint_id: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        screen = ConfirmationScreen(
            title="REMOVE ENDPOINT",
            message=f"Remove {provider_id}/{endpoint_id} from configuration?",
            confirm_label="Remove",
        )
        selected = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if not ConfirmationScreen.confirmed(selected):
            return
        try:
            await self._controller.remove_endpoint(provider_id, endpoint_id)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self.notify(f"Endpoint removed: {provider_id}/{endpoint_id} (next request)")


def _provider_choice(provider: ProviderView, *, disabled: bool = False) -> Choice:
    state = "enabled" if provider.enabled else "disabled"
    return Choice(
        provider.provider_id,
        provider.provider_id,
        marker=SELECTED_CHOICE_MARKER if provider.default else "",
        trailing=f"{provider.provider_type} · {provider.endpoint_count} · {state}",
        disabled=disabled,
    )


def _provider_management_choices(provider: ProviderView) -> tuple[Choice, ...]:
    return (
        Choice("edit", "Edit provider"),
        Choice(
            "toggle",
            "Disable provider" if provider.enabled else "Enable provider",
            disabled=provider.default,
        ),
        Choice("endpoints", "Manage endpoints", trailing=str(provider.endpoint_count)),
        Choice("add-endpoint", "Add endpoint"),
        Choice("remove", "Remove provider"),
    )


def _endpoint_choice(
    endpoint: ProviderEndpointView,
    *,
    available: bool,
    default: bool = False,
) -> Choice:
    return Choice(
        endpoint.endpoint_id,
        endpoint.endpoint_id,
        marker=SELECTED_CHOICE_MARKER if default else "",
        disabled=not available,
    )


def _endpoint_route_choice(
    provider: ProviderView,
    endpoint: ProviderEndpointView,
) -> Choice:
    choice = _endpoint_choice(
        endpoint,
        available=provider.enabled,
        default=provider.default and endpoint.default,
    )
    return Choice(
        f"{provider.provider_id}/{endpoint.endpoint_id}",
        choice.label,
        marker=choice.marker,
        trailing=choice.trailing,
        group=provider.provider_id,
        group_trailing="" if provider.enabled else "unavailable",
        disabled=choice.disabled,
    )


def _provider_action_label(action: str) -> str:
    return {
        "edit": "EDIT",
        "toggle": "ENABLE OR DISABLE",
        "remove": "REMOVE",
    }[action]


def _endpoint_action_label(action: str) -> str:
    return {
        "list": "ENDPOINTS",
        "add": "ADD ENDPOINT",
        "edit": "EDIT ENDPOINT",
        "use": "SET DEFAULT ENDPOINT",
        "remove": "REMOVE ENDPOINT",
        "manage": "ENDPOINTS",
    }[action]
