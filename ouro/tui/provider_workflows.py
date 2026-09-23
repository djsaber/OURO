from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from textual.app import App

from ouro.application.configuration_models import (
    ProviderEndpointView,
    ProviderTemplateUpdateView,
    ProviderView,
)
from ouro.tui.controller import (
    TuiController,
    TuiEndpointConfiguration,
    TuiProviderConfiguration,
)
from ouro.tui.menu_sorting import display_sort_key, model_name_sort_key
from ouro.tui.provider_auth_screens import ProviderAuthenticationScreen
from ouro.tui.provider_screens import (
    CUSTOM_PROVIDER_PROFILES,
    EndpointConfigurationScreen,
    ProviderConfigurationScreen,
    ProviderSetupProfile,
    profile_for_provider,
)
from ouro.tui.screens import Choice, ChoiceScreen, ConfirmationScreen
from ouro.tui.theme import DEFAULT_TUI_THEME, TuiTheme, theme_for
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
        choices = _provider_catalog_choices(catalog.providers, theme=theme_for(self))
        if menu is not None:

            async def refresh_providers() -> tuple[Choice, ...] | None:
                try:
                    refreshed = await self._controller.provider_catalog()
                except Exception as exc:  # noqa: BLE001
                    await self.query_one(ConversationView).add_notice(str(exc), error=True)
                    return None
                return _provider_catalog_choices(
                    refreshed.providers,
                    theme=theme_for(self),
                )

            async def select_provider(provider_id: str) -> tuple[Choice, ...] | None:
                if provider_id == "add-provider":
                    await self._add_provider(menu=menu)
                    return None
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
                on_refresh=refresh_providers,
            )
            return
        selected = await self.push_screen_wait(
            ChoiceScreen(title="PROVIDERS", placeholder="Filter providers…", choices=choices)
        )
        if selected is None:
            return
        if selected == "add-provider":
            await self._add_provider()
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
            {f"custom:{profile.protocol}": profile for profile in CUSTOM_PROVIDER_PROFILES}
        )
        choices = (
            *(
                Choice(
                    f"preset:{template.template_id}",
                    template.display_name,
                    trailing=f"{template.endpoint_count} endpoints",
                    group="Ready to use",
                )
                for template in sorted(
                    templates,
                    key=lambda item: display_sort_key(
                        item.display_name, fallback=item.template_id
                    ),
                )
            ),
            *(
                Choice(
                    f"custom:{profile.protocol}",
                    profile.display_name,
                    trailing=profile.protocol,
                    group="Custom connection",
                )
                for profile in sorted(
                    CUSTOM_PROVIDER_PROFILES,
                    key=lambda item: display_sort_key(
                        item.display_name, fallback=item.protocol
                    ),
                )
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
        screen = ProviderConfigurationScreen(profile, theme=theme_for(self))
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
        self.notify(
            theme_for(self).inline_separator.join(
                (f"Provider added: {provider.display_name}", suffix)
            )
        )

    async def _manage_provider(
        self,
        provider: ProviderView,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        choices = _provider_management_choices(provider, theme=theme_for(self))
        if menu is not None:

            async def refresh_provider_actions() -> tuple[Choice, ...] | None:
                nonlocal provider
                try:
                    catalog = await self._controller.provider_catalog()
                except Exception as exc:  # noqa: BLE001
                    await self.query_one(ConversationView).add_notice(str(exc), error=True)
                    return None
                refreshed = next(
                    (
                        item
                        for item in catalog.providers
                        if item.provider_id == provider.provider_id
                    ),
                    None,
                )
                if refreshed is None:
                    return ()
                provider = refreshed
                return _provider_management_choices(provider, theme=theme_for(self))

            async def select_action(selected: str) -> tuple[Choice, ...] | None:
                nonlocal provider
                if selected == "authentication":
                    await self._manage_provider_authentication(provider, menu=menu)
                elif selected == "edit":
                    await self._edit_provider(provider, menu=menu)
                elif selected == "update-template":
                    saved = await self._update_provider_template(provider)
                    if saved is None:
                        return None
                    provider = saved
                    catalog = await self._controller.provider_catalog()
                    menu.update_parent_choices(
                        _provider_catalog_choices(
                            catalog.providers,
                            theme=theme_for(self),
                        ),
                        initial_value=provider.provider_id,
                    )
                    return _provider_management_choices(provider, theme=theme_for(self))
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
                        _provider_catalog_choices(
                            catalog.providers,
                            theme=theme_for(self),
                        ),
                        initial_value=provider.provider_id,
                    )
                    return _provider_management_choices(provider, theme=theme_for(self))
                elif selected == "endpoints":
                    await self._choose_endpoints(provider, menu=menu)
                elif selected == "remove":
                    if await self._remove_provider(provider, menu=menu):
                        await menu.pop_page_refreshed()
                return None

            menu.push_page(
                title=theme_for(self).inline_separator.join(("PROVIDER", provider.display_name)),
                placeholder="Choose an action…",
                choices=choices,
                on_select=select_action,
                on_refresh=refresh_provider_actions,
            )
            return
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=theme_for(self).inline_separator.join(("PROVIDER", provider.display_name)),
                placeholder="Choose an action…",
                choices=choices,
            )
        )
        if selected == "authentication":
            await self._manage_provider_authentication(provider)
        elif selected == "edit":
            await self._edit_provider(provider)
        elif selected == "update-template":
            await self._update_provider_template(provider)
        elif selected == "toggle":
            await self._set_provider_enabled(provider, enabled=not provider.enabled)
        elif selected == "endpoints":
            await self._choose_endpoints(provider)
        elif selected == "remove":
            await self._remove_provider(provider)

    async def _manage_provider_authentication(
        self,
        provider: ProviderView,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        try:
            service = self._controller.provider_auth()
        except Exception as exc:  # noqa: BLE001 - injected runtimes may omit authentication.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        try:
            catalog = await self._controller.provider_catalog()
        except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        provider_names = {item.provider_id: item.display_name for item in catalog.providers}
        screen = ProviderAuthenticationScreen(
            service, provider.provider_id, provider_names=provider_names, theme=theme_for(self)
        )
        if menu is not None:
            await menu.open_child(screen)
        else:
            await self.push_screen_wait(screen)

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
        screen = ProviderConfigurationScreen(
            profile,
            existing=provider,
            theme=theme_for(self),
        )
        draft = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if draft is None:
            return
        provider_draft = cast(TuiProviderConfiguration, draft)
        try:
            saved = await self._controller.update_provider(
                provider.provider_id,
                provider_draft,
            )
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        self.notify(f"Provider saved: {saved.display_name} (next request)")

    async def _update_provider_template(
        self,
        provider: ProviderView,
    ) -> ProviderView | None:
        try:
            catalog = await self._controller.update_provider_template(provider.provider_id)
        except Exception as exc:  # noqa: BLE001 - configuration errors are user-visible.
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return None
        updated = next(
            item for item in catalog.providers if item.provider_id == provider.provider_id
        )
        report = catalog.template_update
        if report is None:
            await self.query_one(ConversationView).add_notice(
                f"Provider has no preset template: {provider.display_name}",
                error=True,
            )
            return None
        self.notify(_provider_template_update_notice(report, theme=theme_for(self)))
        return updated

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
        provider: ProviderView,
        *,
        menu: ChoiceScreen | None = None,
    ) -> bool:
        if provider.default:
            return False
        screen = ConfirmationScreen(
            title="DELETE PROVIDER",
            message=(
                f"Delete {provider.display_name} and all of its endpoints from your settings?"
            ),
            confirm_label="Delete",
        )
        selected = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if not ConfirmationScreen.confirmed(selected):
            return False
        try:
            catalog = await self._controller.provider_catalog()
            if any(
                item.provider_id == provider.provider_id and item.default
                for item in catalog.providers
            ):
                self.notify("Default provider cannot be deleted", severity="warning")
                return False
            await self._controller.remove_provider(provider.provider_id)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return False
        self.notify(f"Provider deleted: {provider.display_name} (next request)")
        return True

    async def _choose_endpoint_action(
        self,
        action: str,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        if action not in {"list", "add", "use"}:
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
            providers = _sorted_providers(catalog.providers)
            if not providers:
                return
            if menu is not None:
                providers_by_id = {provider.provider_id: provider for provider in providers}

                async def refresh_provider_choices() -> tuple[Choice, ...] | None:
                    try:
                        refreshed = await self._controller.provider_catalog()
                    except Exception as exc:  # noqa: BLE001
                        await self.query_one(ConversationView).add_notice(str(exc), error=True)
                        return None
                    providers_by_id.clear()
                    providers_by_id.update(
                        {provider.provider_id: provider for provider in refreshed.providers}
                    )
                    return tuple(
                        _provider_choice(provider, theme=theme_for(self))
                        for provider in _sorted_providers(tuple(providers_by_id.values()))
                    )

                async def select_provider(provider_id: str) -> tuple[Choice, ...] | None:
                    await self._edit_endpoint(
                        providers_by_id[provider_id],
                        None,
                        menu=menu,
                    )
                    return None

                menu.push_page(
                    title=theme_for(self).inline_separator.join(("ADD ENDPOINT", "PROVIDER")),
                    placeholder="Choose a provider…",
                    choices=tuple(
                        _provider_choice(provider, theme=theme_for(self)) for provider in providers
                    ),
                    on_select=select_provider,
                    on_refresh=refresh_provider_choices,
                )
                return
            provider_id = await self.push_screen_wait(
                ChoiceScreen(
                    title=theme_for(self).inline_separator.join(("ADD ENDPOINT", "PROVIDER")),
                    placeholder="Choose a provider…",
                    choices=tuple(
                        _provider_choice(provider, theme=theme_for(self)) for provider in providers
                    ),
                )
            )
            if provider_id is None:
                return
            provider = next(item for item in providers if item.provider_id == provider_id)
            await self._edit_endpoint(provider, None)
            return

        routes = _endpoint_routes(catalog.providers)
        if not routes:
            return
        default_endpoint = _global_default_endpoint(catalog.providers)
        choices = tuple(
            _endpoint_route_choice(
                provider,
                endpoint,
                default_endpoint=default_endpoint,
                theme=theme_for(self),
            )
            for provider, endpoint in routes.values()
        )

        async def refresh_routes() -> tuple[Choice, ...] | None:
            try:
                refreshed = await self._controller.provider_catalog()
            except Exception as exc:  # noqa: BLE001
                await self.query_one(ConversationView).add_notice(str(exc), error=True)
                return None
            routes.clear()
            routes.update(_endpoint_routes(refreshed.providers))
            default_endpoint = _global_default_endpoint(refreshed.providers)
            return tuple(
                _endpoint_route_choice(
                    item,
                    candidate,
                    default_endpoint=default_endpoint,
                    theme=theme_for(self),
                )
                for item, candidate in routes.values()
            )

        if action == "use":

            async def set_default_route(route: str) -> tuple[Choice, ...] | None:
                provider, endpoint = routes[route]
                if not await self._use_endpoint(provider.provider_id, endpoint.endpoint_id):
                    return None
                return await refresh_routes()

            if menu is not None:
                menu.push_page(
                    title="SET DEFAULT ENDPOINT",
                    placeholder="Filter endpoints…",
                    choices=choices,
                    on_select=set_default_route,
                    on_refresh=refresh_routes,
                )
            else:
                await self.push_screen_wait(
                    ChoiceScreen(
                        title="SET DEFAULT ENDPOINT",
                        placeholder="Filter endpoints…",
                        choices=choices,
                        on_select=set_default_route,
                        on_refresh=refresh_routes,
                    )
                )
            return

        if menu is not None:

            async def select_route(route: str) -> tuple[Choice, ...] | None:
                provider, endpoint = routes[route]
                await self._manage_endpoint(provider, endpoint, menu=menu)
                return None

            menu.push_page(
                title="ENDPOINTS",
                placeholder="Filter endpoints…",
                choices=choices,
                on_select=select_route,
                on_refresh=refresh_routes,
            )
            return

        route = await self.push_screen_wait(
            ChoiceScreen(
                title="ENDPOINTS",
                placeholder="Filter endpoints…",
                choices=choices,
            )
        )
        if route is None:
            return
        provider, endpoint = routes[route]
        await self._manage_endpoint(provider, endpoint)

    async def _choose_endpoints(
        self,
        provider: ProviderView,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        try:
            catalog = await self._controller.provider_endpoints(provider.provider_id)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        candidates = _sorted_endpoints(catalog.endpoints)
        choices = _provider_endpoint_choices(
            provider,
            candidates,
            include_add=True,
            theme=theme_for(self),
        )
        if menu is not None:
            endpoints = {endpoint.endpoint_id: endpoint for endpoint in candidates}

            async def refresh_endpoints() -> tuple[Choice, ...] | None:
                nonlocal provider
                try:
                    refreshed = await self._controller.provider_catalog()
                except Exception as exc:  # noqa: BLE001
                    await self.query_one(ConversationView).add_notice(str(exc), error=True)
                    return None
                current_provider = next(
                    (
                        item
                        for item in refreshed.providers
                        if item.provider_id == provider.provider_id
                    ),
                    None,
                )
                if current_provider is None:
                    return ()
                provider = current_provider
                refreshed_candidates = _sorted_endpoints(provider.endpoints)
                endpoints.clear()
                endpoints.update(
                    {endpoint.endpoint_id: endpoint for endpoint in refreshed_candidates}
                )
                return _provider_endpoint_choices(
                    provider,
                    refreshed_candidates,
                    include_add=True,
                    theme=theme_for(self),
                )

            async def select_endpoint(endpoint_id: str) -> tuple[Choice, ...] | None:
                if endpoint_id == "add-endpoint":
                    await self._edit_endpoint(provider, None, menu=menu)
                    return await refresh_endpoints()
                endpoint = endpoints[endpoint_id]
                await self._manage_endpoint(provider, endpoint, menu=menu)
                return None

            menu.push_page(
                title=theme_for(self).inline_separator.join(
                    ("ENDPOINTS", provider.display_name)
                ),
                placeholder="Filter endpoints…",
                choices=choices,
                on_select=select_endpoint,
                on_refresh=refresh_endpoints,
            )
            return
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=theme_for(self).inline_separator.join(
                    ("ENDPOINTS", provider.display_name)
                ),
                placeholder="Filter endpoints…",
                choices=choices,
            )
        )
        if selected is None:
            return
        if selected == "add-endpoint":
            await self._edit_endpoint(provider, None)
            return
        endpoint = next(item for item in candidates if item.endpoint_id == selected)
        await self._manage_endpoint(provider, endpoint)

    async def _manage_endpoint(
        self,
        provider: ProviderView,
        endpoint: ProviderEndpointView,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        choices = _endpoint_management_choices(
            provider,
            endpoint,
            theme=theme_for(self),
        )
        if menu is not None:

            async def refresh_endpoint_actions() -> tuple[Choice, ...] | None:
                nonlocal provider, endpoint
                try:
                    catalog = await self._controller.provider_catalog()
                except Exception as exc:  # noqa: BLE001
                    await self.query_one(ConversationView).add_notice(str(exc), error=True)
                    return None
                refreshed_provider = next(
                    (
                        item
                        for item in catalog.providers
                        if item.provider_id == provider.provider_id
                    ),
                    None,
                )
                if refreshed_provider is None:
                    return ()
                refreshed_endpoint = next(
                    (
                        item
                        for item in refreshed_provider.endpoints
                        if item.endpoint_id == endpoint.endpoint_id
                    ),
                    None,
                )
                if refreshed_endpoint is None:
                    return ()
                provider = refreshed_provider
                endpoint = refreshed_endpoint
                return _endpoint_management_choices(
                    provider,
                    endpoint,
                    theme=theme_for(self),
                )

            async def select_action(selected: str) -> tuple[Choice, ...] | None:
                if selected == "edit":
                    await self._edit_endpoint(provider, endpoint, menu=menu)
                elif selected == "use":
                    if await self._use_endpoint(provider.provider_id, endpoint.endpoint_id):
                        return await refresh_endpoint_actions()
                elif selected == "remove":
                    if await self._remove_endpoint(
                        provider,
                        endpoint,
                        menu=menu,
                    ):
                        await menu.pop_page_refreshed()
                return None

            menu.push_page(
                title=theme_for(self).inline_separator.join(
                    ("ENDPOINT", endpoint.display_name or endpoint.model_id)
                ),
                placeholder="Choose an action…",
                choices=choices,
                on_select=select_action,
                on_refresh=refresh_endpoint_actions,
            )
            return
        selected = await self.push_screen_wait(
            ChoiceScreen(
                title=theme_for(self).inline_separator.join(
                    ("ENDPOINT", endpoint.display_name or endpoint.model_id)
                ),
                placeholder="Choose an action…",
                choices=choices,
            )
        )
        if selected == "edit":
            await self._edit_endpoint(provider, endpoint)
        elif selected == "use":
            await self._use_endpoint(provider.provider_id, endpoint.endpoint_id)
        elif selected == "remove":
            await self._remove_endpoint(provider, endpoint)

    async def _edit_endpoint(
        self,
        provider: ProviderView,
        existing: ProviderEndpointView | None,
        *,
        menu: ChoiceScreen | None = None,
    ) -> None:
        screen = EndpointConfigurationScreen(
            provider,
            existing=existing,
            theme=theme_for(self),
        )
        draft = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if draft is None:
            return
        endpoint_draft = cast(TuiEndpointConfiguration, draft)
        try:
            saved = (
                await self._controller.add_endpoint(provider.provider_id, endpoint_draft)
                if existing is None
                else await self._controller.update_endpoint(
                    provider.provider_id,
                    existing.endpoint_id,
                    endpoint_draft,
                )
            )
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return
        action = "added" if existing is None else "saved"
        self.notify(
            f"Endpoint {action}: {saved.display_name or saved.model_id} (next request)"
        )

    async def _use_endpoint(self, provider_id: str, endpoint_id: str) -> bool:
        try:
            await self._controller.use_endpoint(provider_id, endpoint_id)
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return False
        return True

    async def _remove_endpoint(
        self,
        provider: ProviderView,
        endpoint: ProviderEndpointView,
        *,
        menu: ChoiceScreen | None = None,
    ) -> bool:
        if _is_global_default_endpoint(provider, endpoint):
            return False
        endpoint_label = endpoint.display_name or endpoint.model_id
        screen = ConfirmationScreen(
            title="DELETE ENDPOINT",
            message=f"Delete {endpoint_label} from {provider.display_name}?",
            confirm_label="Delete",
        )
        selected = await (
            menu.open_child(screen) if menu is not None else self.push_screen_wait(screen)
        )
        if not ConfirmationScreen.confirmed(selected):
            return False
        try:
            catalog = await self._controller.provider_catalog()
            if _global_default_endpoint(catalog.providers) == (
                f"{provider.provider_id}/{endpoint.endpoint_id}"
            ):
                self.notify("Default endpoint cannot be deleted", severity="warning")
                return False
            await self._controller.remove_endpoint(
                provider.provider_id,
                endpoint.endpoint_id,
            )
        except Exception as exc:  # noqa: BLE001
            await self.query_one(ConversationView).add_notice(str(exc), error=True)
            return False
        self.notify(f"Endpoint deleted: {endpoint_label} (next request)")
        return True


def _provider_choice(
    provider: ProviderView,
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> Choice:
    return Choice(
        provider.provider_id,
        provider.display_name,
        accent=None if provider.enabled else theme.text_lowlight,
        trailing=provider.provider_type,
        trailing_badge="default" if provider.default else "",
    )


def _provider_catalog_choices(
    providers: Sequence[ProviderView],
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[Choice, ...]:
    ordered = _sorted_providers(providers)
    return (
        *(_provider_choice(provider, theme=theme) for provider in ordered),
        Choice("add-provider", "+"),
    )


def _provider_management_choices(
    provider: ProviderView,
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[Choice, ...]:
    choices = [Choice("edit", "Edit"), Choice("authentication", "Authentication")]
    if provider.notice:
        choices.append(
            Choice("service-notice", "Service limits", description=provider.notice, disabled=True)
        )
    if provider.template_id is not None:
        choices.append(
            Choice(
                "update-template",
                "Update",
                trailing="Apply latest preset",
            )
        )
    choices.extend(
        (
            Choice(
                "toggle",
                "Disable" if provider.enabled else "Enable",
                disabled=provider.default,
            ),
            Choice("endpoints", "Endpoints"),
            Choice(
                "remove",
                "Delete",
                accent=None if provider.default else theme.danger,
                disabled=provider.default,
                destructive=not provider.default,
            ),
        )
    )
    return tuple(choices)


def _provider_template_update_notice(
    update: ProviderTemplateUpdateView,
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> str:
    mode = f"updated to r{update.to_revision}"
    changes = (
        f"{len(update.added_endpoints)} added",
        f"{len(update.updated_endpoints)} updated",
        f"{len(update.removed_endpoints)} deleted",
        f"{len(update.preserved_endpoints)} preserved",
        f"{len(update.conflicted_endpoints)} conflicts",
    )
    return theme.inline_separator.join((f"Provider preset {mode}: {update.template_id}", *changes))


def _endpoint_choice(
    endpoint: ProviderEndpointView,
    *,
    available: bool,
    default: bool,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> Choice:
    trailing = _endpoint_capabilities(endpoint)
    return Choice(
        endpoint.endpoint_id,
        endpoint.display_name or endpoint.model_id,
        trailing=theme.inline_separator.join(trailing),
        trailing_badge="default" if default else "",
        disabled=not available,
    )


def _provider_endpoint_choices(
    provider: ProviderView,
    endpoints: Sequence[ProviderEndpointView],
    *,
    include_add: bool,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[Choice, ...]:
    choices = tuple(
        _endpoint_choice(
            endpoint,
            available=provider.enabled,
            default=_is_global_default_endpoint(provider, endpoint),
            theme=theme,
        )
        for endpoint in endpoints
    )
    return (*choices, Choice("add-endpoint", "+")) if include_add else choices


def _sorted_endpoints(
    endpoints: Sequence[ProviderEndpointView],
) -> tuple[ProviderEndpointView, ...]:
    return tuple(
        sorted(
            endpoints,
            key=lambda endpoint: (
                model_name_sort_key(endpoint.display_name, model_id=endpoint.model_id),
                endpoint.endpoint_id.casefold(),
            ),
            reverse=True,
        )
    )


def _endpoint_management_choices(
    provider: ProviderView,
    endpoint: ProviderEndpointView,
    *,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> tuple[Choice, ...]:
    choices = [Choice("edit", "Edit")]
    is_default = _is_global_default_endpoint(provider, endpoint)
    if not is_default:
        choices.append(Choice("use", "Set as default"))
    choices.append(
        Choice(
            "remove",
            "Delete",
            accent=None if is_default else theme.danger,
            disabled=is_default,
            destructive=not is_default,
        )
    )
    return tuple(choices)


def _endpoint_route_choice(
    provider: ProviderView,
    endpoint: ProviderEndpointView,
    *,
    default_endpoint: str | None,
    theme: TuiTheme = DEFAULT_TUI_THEME,
) -> Choice:
    choice = _endpoint_choice(
        endpoint,
        available=provider.enabled,
        default=default_endpoint == f"{provider.provider_id}/{endpoint.endpoint_id}",
        theme=theme,
    )
    return Choice(
        f"{provider.provider_id}/{endpoint.endpoint_id}",
        choice.label,
        trailing=choice.trailing,
        trailing_badge=choice.trailing_badge,
        group=provider.display_name,
        group_trailing="" if provider.enabled else "unavailable",
        disabled=choice.disabled,
    )


def _endpoint_capabilities(endpoint: ProviderEndpointView) -> tuple[str, ...]:
    return tuple(
        capability
        for capability, supported in (
            ("reasoning", endpoint.reasoning.capability.activation != "unsupported"),
            ("vision", endpoint.supports_vision),
        )
        if supported
    )


def _global_default_endpoint(providers: Sequence[ProviderView]) -> str | None:
    provider = next((candidate for candidate in providers if candidate.default), None)
    if provider is None or provider.default_endpoint is None:
        return None
    return f"{provider.provider_id}/{provider.default_endpoint}"


def _is_global_default_endpoint(
    provider: ProviderView,
    endpoint: ProviderEndpointView,
) -> bool:
    return provider.default and provider.default_endpoint == endpoint.endpoint_id


def _sorted_providers(providers: Sequence[ProviderView]) -> tuple[ProviderView, ...]:
    return tuple(
        sorted(
            providers,
            key=lambda provider: display_sort_key(
                provider.display_name, fallback=provider.provider_id
            ),
        )
    )


def _endpoint_routes(
    providers: Sequence[ProviderView],
) -> dict[str, tuple[ProviderView, ProviderEndpointView]]:
    return {
        f"{provider.provider_id}/{endpoint.endpoint_id}": (provider, endpoint)
        for provider in _sorted_providers(providers)
        for endpoint in _sorted_endpoints(provider.endpoints)
    }
