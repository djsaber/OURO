"""TUI application scenarios: providers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from rich.text import Text
from textual.widgets import Input, OptionList, Static

from ouro.application.configuration.facade import ConfigurationFacade
from ouro.application.contracts.models import AvailableModel
from ouro.application.runtime.environment import RuntimeEnvironment
from ouro.application.runtime.service import RuntimeService
from ouro.config import reasoning_effort_preset
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest
from ouro.local_application import LocalApplication
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.composer.popup import CommandPopup
from ouro.tui.conversation.turn import AssistantTurn
from ouro.tui.features.providers.model_menus import model_choices
from ouro.tui.features.providers.screens import EndpointConfigurationScreen
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.confirmation import ConfirmationScreen
from ouro.tui.view_models import TuiEndpointConfiguration, TuiProviderConfiguration
from ouro.tui.widgets.activity import ActivityIndicator
from tests.tui.app.providers import BlockingStreamProvider
from tests.tui.app.support import _application, _blocking_application
from tests.tui.helpers import choose_menu, command_popup_text, run_ready_app, wait_for_menu_action
from tests.tui.helpers import wait_for_screen as _wait_for_screen
from tests.tui.helpers import wait_until as _wait_until


@pytest.mark.asyncio
async def test_first_markdown_chunk_renders_while_provider_is_still_running(
    tmp_path: Path,
) -> None:
    provider = BlockingStreamProvider()
    app = OuroTuiApp(application=_blocking_application(tmp_path, provider))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.click("#composer")
        try:
            await pilot.press("s", "t", "r", "e", "a", "m", "enter")
            await _wait_until(
                lambda: app._projection is not None and app._projection.assistant_text == "first ",
                pilot,
                description="first streamed chunk",
            )

            assert app._projection is not None
            assert app._projection.assistant_text == "first "
            assert app._projection.activity.status != "completed"
            assert app.query_one(AssistantTurn).source == "first "
        finally:
            provider.release.set()
        await _wait_until(
            lambda: app._projection is not None and app._projection.activity.status == "completed",
            pilot,
            description="completed streamed run",
        )

        assert app._projection is not None
        assert app._projection.assistant_text == "first second"
        assert app._projection.activity.status == "completed"


@pytest.mark.asyncio
async def test_status_line_default_reflects_endpoint_default(tmp_path: Path) -> None:
    # An endpoint whose default is disabled shows nothing under a default intent.
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(
                model_id="thinking-model",
                provider_id="faux",
                reasoning=ReasoningCapability(
                    activation="toggle",
                    supported_efforts=("low", "high"),
                    default=ReasoningRequest(mode="disabled", effort="low"),
                ),
            ),
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    app = OuroTuiApp(application=LocalApplication.from_runtime(runtime))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        indicator = app.query_one(ActivityIndicator)
        # The default intent resolves to the endpoint's disabled default, so the
        # thinking indicator stays hidden.
        assert indicator._reasoning is None


@pytest.mark.asyncio
async def test_provider_enable_action_is_owned_by_provider_submenu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    created = configuration.add_provider(
        display_name="Secondary",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://secondary.example.invalid/v1",
        enabled=False,
    )
    provider_id = created.changed_provider_id
    assert provider_id is not None
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Manage providers", title="PROVIDERS")
        catalog_started, release_catalog = asyncio.Event(), asyncio.Event()
        provider_catalog = app._controller.provider_catalog

        async def delayed_catalog():
            catalog_started.set()
            await release_catalog.wait()
            return await provider_catalog()

        monkeypatch.setattr(app._controller, "provider_catalog", delayed_catalog)
        try:
            await choose_menu(pilot, "Secondary")
            await asyncio.wait_for(catalog_started.wait(), timeout=15)
            # An idle message pump does not imply that the catalog read has finished.
            await pilot.pause()
            assert app.screen.page_title == "PROVIDERS"
            assert app.screen.query_one("#choice-filter", Input).has_focus
        finally:
            release_catalog.set()
        await _wait_for_screen(app, ChoiceScreen, title="PROVIDER • Secondary")

        assert app.screen.query_one("#choice-title", Static).render().plain == (
            "PROVIDER • Secondary"
        )
        toggle = next(choice for choice in app.screen._choices if choice.value == "toggle")
        assert toggle.label == "Enable"

        options = app.screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("toggle")
        options.focus()
        await pilot.press("enter")
        await _wait_until(
            lambda: (
                next(choice for choice in app.screen._choices if choice.value == "toggle").label
                == "Disable"
            ),
            pilot,
            description="enabled provider reflected in its submenu",
        )
        assert ConfigurationFacade(tmp_path).provider(provider_id).enabled is True

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="PROVIDERS")
        provider_choice = next(
            choice for choice in app.screen._choices if choice.value == provider_id
        )
        assert provider_choice.trailing == "custom"
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PROVIDER • Secondary")
        options = app.screen.query_one("#choice-list", OptionList)
        await pilot.click(options, offset=(4, options.get_option_index("toggle")))
        await _wait_until(
            lambda: (
                next(choice for choice in app.screen._choices if choice.value == "toggle").label
                == "Enable"
            ),
            pilot,
            description="disabled provider reflected in its submenu",
        )
        assert ConfigurationFacade(tmp_path).provider(provider_id).enabled is False

        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="PROVIDERS")
        provider_choice = next(
            choice for choice in app.screen._choices if choice.value == provider_id
        )
        assert provider_choice.trailing == "custom"
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_provider_submenu_updates_from_latest_preset_in_place(tmp_path: Path) -> None:
    configuration = ConfigurationFacade(tmp_path)
    created = configuration.add_provider(display_name="Primary", provider_type="openai")
    provider_id = created.changed_provider_id
    assert provider_id is not None
    template_path = tmp_path / "config" / "provider-templates.json"
    root = json.loads(template_path.read_text(encoding="utf-8"))
    template = root["templates"]["openai"]
    initial_revision = template["revision"]
    template["revision"] = initial_revision + 1
    endpoint = next(item for item in template["endpoints"] if item["endpoint_id"] == "gpt-5.6-sol")
    endpoint["context_window"] = 2_000_000
    template_path.write_text(json.dumps(root), encoding="utf-8")
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Manage providers", title="PROVIDERS")
        await choose_menu(pilot, "Primary", title="PROVIDER • Primary")
        menu = app.screen

        update = next(choice for choice in app.screen._choices if choice.value == "update-template")
        assert update.label == "Update"
        assert update.trailing == "Apply latest preset"
        options = app.screen.query_one("#choice-list", OptionList)
        options.highlighted = options.get_option_index("update-template")
        options.focus()
        await pilot.press("enter")
        await wait_for_menu_action(pilot, menu)

        assert (
            next(
                endpoint.context_window
                for endpoint in ConfigurationFacade(tmp_path).provider(provider_id).endpoints
                if endpoint.model_id == "gpt-5.6-sol"
            )
            == 2_000_000
        )
        refreshed = next(
            choice for choice in app.screen._choices if choice.value == "update-template"
        )
        assert refreshed.trailing == "Apply latest preset"


@pytest.mark.asyncio
async def test_provider_catalog_refreshes_before_returning_from_submenu(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    created = configuration.add_provider(
        display_name="Custom",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://example.invalid/v1",
    )
    provider_id = created.changed_provider_id
    assert provider_id is not None
    configuration.add_provider(
        display_name="中文供应商",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://zh.example.invalid/v1",
        enabled=False,
    )
    configuration.add_provider(
        display_name="alpha",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://alpha.example.invalid/v1",
    )
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one("#choice-filter", Input).value = "Manage providers"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PROVIDERS")
        menu = app.screen
        assert [choice.label for choice in menu._choices] == [
            "alpha",
            "Custom",
            "中文供应商",
            "+",
        ]
        options = menu.query_one(OptionList)
        options.highlighted = options.get_option_index(provider_id)
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PROVIDER • Custom")

        await app._controller.add_endpoint(
            provider_id,
            TuiEndpointConfiguration(
                display_name="New model",
                model_id="provider-model-id",
                context_window=128_000,
                max_output_tokens=8192,
            ),
        )
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="PROVIDERS")

        assert app.screen is menu
        assert menu.query_one(OptionList) is options
        refreshed = next(choice for choice in menu._choices if choice.value == provider_id)
        assert refreshed.trailing == "custom"
        assert menu._choices[-1].label == "+"
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_default_provider_and_endpoint_show_badges_and_disable_deletion(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    created = configuration.add_provider(
        display_name="Primary",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://primary.example.invalid/v1",
    )
    provider_id = created.changed_provider_id
    assert provider_id is not None
    configuration.add_endpoint(provider_id, display_name="Chat", model="primary-model")
    configuration.use_provider(provider_id)
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Manage providers", title="PROVIDERS")
        provider_choice = next(
            choice for choice in app.screen._choices if choice.value == provider_id
        )
        assert provider_choice.marker == ""
        assert provider_choice.trailing_badge == "default"
        assert provider_choice.trailing == "custom"
        provider_options = app.screen.query_one(OptionList)
        assert any(
            "default" in (line := provider_options.render_line(y)).text
            and line.text.index("default") < line.text.index("custom")
            for y in range(provider_options.size.height)
        )
        await choose_menu(pilot, "Primary", title="PROVIDER • Primary")

        toggle = next(choice for choice in app.screen._choices if choice.value == "toggle")
        assert toggle.label == "Disable"
        assert toggle.disabled is True
        option = app.screen.query_one("#choice-list", OptionList).get_option("toggle")
        assert option.disabled is True
        remove = next(choice for choice in app.screen._choices if choice.value == "remove")
        assert remove.disabled is True
        assert app.screen.query_one("#choice-list", OptionList).get_option("remove").disabled
        app.screen.query_one("#choice-filter", Input).value = "Delete"
        await pilot.pause()
        assert app.screen.query_one(OptionList).highlighted is None
        await pilot.press("enter")
        assert app.screen.query_one("#choice-title", Static).render().plain == "PROVIDER • Primary"
        app.screen.query_one("#choice-filter", Input).value = "Endpoints"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="ENDPOINTS • Primary")
        endpoint = next(choice for choice in app.screen._choices if choice.label == "Chat")
        assert endpoint.marker == ""
        assert endpoint.trailing_badge == "default"
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="ENDPOINT • Chat")
        remove = next(choice for choice in app.screen._choices if choice.value == "remove")
        assert remove.disabled is True
        assert app.screen.query_one("#choice-list", OptionList).get_option("remove").disabled
        app.screen.query_one("#choice-filter", Input).value = "Delete"
        await pilot.pause()
        assert app.screen.query_one(OptionList).highlighted is None
        await pilot.press("enter")
        assert app.screen.query_one("#choice-title", Static).render().plain == "ENDPOINT • Chat"


@pytest.mark.parametrize(
    ("action_label", "title"),
    (
        ("Manage endpoints", "ENDPOINTS"),
        ("Set default endpoint", "SET DEFAULT ENDPOINT"),
    ),
)
@pytest.mark.asyncio
async def test_endpoint_actions_flatten_routes_grouped_by_provider(
    tmp_path: Path,
    action_label: str,
    title: str,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    routes = {}
    for provider_name in ("primary", "secondary"):
        created = configuration.add_provider(
            display_name=provider_name.title(),
            provider_type="custom",
            protocol="openai-chat-completions",
            base_url=f"https://{provider_name}.example.invalid/v1",
            enabled=provider_name == "primary",
        )
        provider_id = created.changed_provider_id
        assert provider_id is not None
        endpoints = configuration.add_endpoint(
            provider_id,
            display_name="Shared",
            model=f"{provider_name}-model",
            supports_image_input=True,
            reasoning=reasoning_effort_preset("openai-chat-completions-effort"),
        )
        endpoint_id = endpoints.changed_endpoint_id
        assert endpoint_id is not None
        routes[provider_name] = (provider_id, endpoint_id)
    configuration.use_provider(routes["primary"][0])
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, action_label, title=title)

        assert app.screen.query_one("#choice-title", Static).render().plain == title
        choices = {choice.value: choice for choice in app.screen._choices}
        primary_key = "/".join(routes["primary"])
        secondary_key = "/".join(routes["secondary"])
        assert set(choices) == {primary_key, secondary_key}
        assert {choice.label for choice in choices.values()} == {"Shared"}
        assert choices[primary_key].marker == ""
        assert choices[secondary_key].marker == ""
        assert {choice.group for choice in choices.values()} == {"Primary", "Secondary"}
        assert all(choice.description == "" for choice in app.screen._choices)
        separator = app.tui_theme.inline_separator
        assert choices[primary_key].trailing == separator.join(("image", "reasoning"))
        assert choices[primary_key].trailing_badge == "default"
        assert choices[secondary_key].trailing == separator.join(("image", "reasoning"))
        assert choices[secondary_key].trailing_badge == ""
        internal_endpoint_ids = {endpoint_id for _, endpoint_id in routes.values()}
        assert all(
            all(endpoint_id not in choice.trailing for endpoint_id in internal_endpoint_ids)
            for choice in choices.values()
        )
        assert choices[primary_key].group_trailing == ""
        assert choices[secondary_key].group_trailing == "unavailable"
        assert choices[primary_key].disabled is False
        assert choices[secondary_key].disabled is True
        options = app.screen.query_one(OptionList)
        endpoint_options = {
            str(option.id): option
            for index in range(options.option_count)
            if (option := options.get_option_at_index(index)).id is not None
        }
        assert endpoint_options[primary_key].disabled is False
        assert endpoint_options[secondary_key].disabled is True
        assert any(
            "default" in (line := options.render_line(y)).text
            and line.text.index("default") < line.text.index("reasoning")
            for y in range(options.size.height)
        )
        app.screen.query_one("#choice-filter", Input).value = "Secondary"
        await pilot.pause()
        assert app.screen.query_one(OptionList).highlighted is None
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ChoiceScreen)
        await pilot.press("escape")


@pytest.mark.parametrize(
    ("manage_label", "target_label", "confirm_title"),
    (
        (
            "Manage providers",
            "Custom",
            "CONFIRM DELETE PROVIDER",
        ),
        (
            "Manage endpoints",
            "Chat",
            "CONFIRM DELETE ENDPOINT",
        ),
    ),
)
@pytest.mark.asyncio
async def test_provider_deletions_use_shared_menu_confirmation(
    tmp_path: Path,
    manage_label: str,
    target_label: str,
    confirm_title: str,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    created = configuration.add_provider(
        display_name="Custom",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://custom.example.invalid/v1",
    )
    provider_id = created.changed_provider_id
    assert provider_id is not None
    endpoints = configuration.add_endpoint(provider_id, display_name="Chat", model="custom-model")
    endpoint_id = endpoints.changed_endpoint_id
    assert endpoint_id is not None
    message = (
        "Delete Custom and all of its endpoints from your settings?"
        if confirm_title == "CONFIRM DELETE PROVIDER"
        else "Delete Chat from Custom?"
    )
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        catalog_title = "PROVIDERS" if manage_label == "Manage providers" else "ENDPOINTS"
        detail_title = (
            "PROVIDER • Custom" if manage_label == "Manage providers" else "ENDPOINT • Chat"
        )
        await choose_menu(pilot, manage_label, title=catalog_title)
        await choose_menu(pilot, target_label, title=detail_title)
        menu = app.screen

        remove = next(choice for choice in app.screen._choices if choice.value == "remove")
        assert remove.label == "Delete"
        assert remove.destructive
        app.screen.query_one("#choice-filter", Input).value = "Delete"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ConfirmationScreen, title=confirm_title)

        assert isinstance(app.screen, ConfirmationScreen)
        assert app.screen.query_one("#choice-title", Static).render().plain == confirm_title
        assert [choice.label for choice in app.screen._choices] == ["Cancel", "Delete"]
        assert app.screen._choices[1].description == message
        options = app.screen.query_one(OptionList)
        assert options.highlighted == 0
        assert options.has_focus
        confirm = options.get_option(ConfirmationScreen.CONFIRM_VALUE).prompt
        assert isinstance(confirm, Text)
        assert message in confirm.plain

        await pilot.press("down")
        assert any("Delete" in options.render_line(y).text for y in range(options.size.height))
        await pilot.press("up")

        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title=detail_title)
        await wait_for_menu_action(pilot, menu)
        assert app.screen is menu
        assert not isinstance(app.screen, ConfirmationScreen)
        assert ConfigurationFacade(tmp_path).provider(provider_id) is not None


@pytest.mark.asyncio
async def test_provider_management_returns_to_catalog_after_deletion(tmp_path: Path) -> None:
    configuration = ConfigurationFacade(tmp_path)
    created = configuration.add_provider(
        display_name="Custom",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://custom.example.invalid/v1",
    )
    provider_id = created.changed_provider_id
    assert provider_id is not None
    configuration.add_endpoint(provider_id, display_name="Chat", model="custom-model")
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one("#choice-filter", Input).value = "Manage providers"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PROVIDERS")
        app.screen.query_one("#choice-filter", Input).value = "Custom"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PROVIDER • Custom")

        assert [choice.label for choice in app.screen._choices] == [
            "Edit",
            "Authentication",
            "Endpoints",
            "Disable",
            "Delete",
        ]
        options = app.screen.query_one(OptionList)
        options.highlighted = options.get_option_index("remove")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, ConfirmationScreen, title="CONFIRM DELETE PROVIDER")
        await pilot.press("down", "enter")
        await _wait_for_screen(app, ChoiceScreen, title="PROVIDERS")

        assert all(choice.value != provider_id for choice in app.screen._choices)
        assert all(
            provider.provider_id != provider_id
            for provider in ConfigurationFacade(tmp_path).provider_catalog().providers
        )


@pytest.mark.asyncio
async def test_endpoint_management_returns_to_catalog_after_deletion(tmp_path: Path) -> None:
    configuration = ConfigurationFacade(tmp_path)
    created = configuration.add_provider(
        display_name="Custom",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://custom.example.invalid/v1",
    )
    provider_id = created.changed_provider_id
    assert provider_id is not None
    chat = configuration.add_endpoint(
        provider_id,
        display_name="Chat",
        model="chat-model",
        supports_image_input=True,
        reasoning=reasoning_effort_preset("openai-chat-completions-effort"),
    )
    keep = configuration.add_endpoint(provider_id, display_name="Keep", model="keep-model")
    unnamed = configuration.add_endpoint(provider_id, model="alpha-model")
    chinese = configuration.add_endpoint(
        provider_id,
        display_name="中文",
        model="zh-model",
    )
    assert chat.changed_endpoint_id is not None and keep.changed_endpoint_id is not None
    assert unnamed.changed_endpoint_id is not None and chinese.changed_endpoint_id is not None
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one("#choice-filter", Input).value = "Manage providers"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PROVIDERS")
        app.screen.query_one("#choice-filter", Input).value = "Custom"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PROVIDER • Custom")
        app.screen.query_one("#choice-filter", Input).value = "Endpoints"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="ENDPOINTS • Custom")

        assert [choice.label for choice in app.screen._choices] == [
            "中文",
            "Keep",
            "Chat",
            "alpha-model",
            "+",
        ]
        assert next(
            choice for choice in app.screen._choices if choice.value == chat.changed_endpoint_id
        ).trailing == app.tui_theme.inline_separator.join(("image", "reasoning"))
        app.screen.query_one("#choice-filter", Input).value = "Chat"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="ENDPOINT • Chat")

        assert [choice.label for choice in app.screen._choices] == [
            "Edit",
            "Set as default",
            "Delete",
        ]
        remove = next(choice for choice in app.screen._choices if choice.value == "remove")
        assert remove.destructive
        options = app.screen.query_one(OptionList)
        options.highlighted = options.get_option_index("remove")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, ConfirmationScreen, title="CONFIRM DELETE ENDPOINT")
        await pilot.press("down", "enter")
        await _wait_for_screen(app, ChoiceScreen, title="ENDPOINTS • Custom")

        values = {choice.value for choice in app.screen._choices}
        assert chat.changed_endpoint_id not in values
        assert keep.changed_endpoint_id in values
        assert app.screen._choices[-1].label == "+"
        app.screen.query_one("#choice-filter", Input).value = "+"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, EndpointConfigurationScreen, title="ADD ENDPOINT")
        await pilot.press("escape")
        await _wait_for_screen(app, ChoiceScreen, title="ENDPOINTS • Custom")


@pytest.mark.asyncio
async def test_setting_default_endpoint_also_switches_default_provider(tmp_path: Path) -> None:
    configuration = ConfigurationFacade(tmp_path)
    routes = {}
    for provider_name in ("primary", "secondary"):
        created = configuration.add_provider(
            display_name=provider_name.title(),
            provider_type="custom",
            protocol="openai-chat-completions",
            base_url=f"https://{provider_name}.example.invalid/v1",
        )
        provider_id = created.changed_provider_id
        assert provider_id is not None
        endpoints = configuration.add_endpoint(
            provider_id,
            display_name=f"{provider_name.title()} chat",
            model=f"{provider_name}-model",
        )
        endpoint_id = endpoints.changed_endpoint_id
        assert endpoint_id is not None
        routes[provider_name] = (provider_id, endpoint_id)
    configuration.use_provider(routes["primary"][0])
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34), notifications=True) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "Set default endpoint", title="SET DEFAULT ENDPOINT")
        app.screen.query_one("#choice-filter", Input).value = "Secondary chat"
        await pilot.pause()
        default_endpoint_screen = app.screen
        await pilot.press("enter")
        await _wait_until(
            lambda: any(
                choice.value == "/".join(routes["secondary"]) and choice.trailing_badge == "default"
                for choice in default_endpoint_screen._choices
            ),
            pilot,
            description="committed default endpoint reflected in the menu",
        )

        assert app.screen is default_endpoint_screen
        assert isinstance(app.screen, ChoiceScreen)
        assert app.screen.query_one("#choice-filter", Input).value == "Secondary chat"
        choices = {choice.value: choice for choice in app.screen._choices}
        primary_key = "/".join(routes["primary"])
        secondary_key = "/".join(routes["secondary"])
        assert choices[primary_key].label == "Primary chat"
        assert choices[primary_key].marker == ""
        assert choices[secondary_key].label == "Secondary chat"
        assert choices[secondary_key].marker == ""
        assert choices[primary_key].trailing == ""
        assert choices[secondary_key].trailing == ""
        assert choices[primary_key].trailing_badge == ""
        assert choices[secondary_key].trailing_badge == "default"
        assert sum(choice.trailing_badge == "default" for choice in choices.values()) == 1
        catalog = ConfigurationFacade(tmp_path).provider_catalog()
        assert catalog.default_provider_id == routes["secondary"][0]
        assert (
            ConfigurationFacade(tmp_path).provider(routes["secondary"][0]).default_endpoint
            == routes["secondary"][1]
        )
        assert not list(app.screen.query_one("#textual-toastrack").query("Toast"))
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_model_command_refreshes_endpoints_configured_after_tui_start(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=LocalApplication.build(tmp_path))

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        provider = await app._controller.add_provider(
            TuiProviderConfiguration(
                display_name="Hot Provider",
                provider_type="custom",
                protocol="openai-chat-completions",
                base_url="https://models.example.invalid/v1",
                api_path="/chat/completions",
                api_key=None,
                api_key_env=None,
                timeout=60.0,
            )
        )
        endpoint = await app._controller.add_endpoint(
            provider.provider_id,
            TuiEndpointConfiguration(
                display_name="Hot Model",
                model_id="provider-model-id",
                context_window=128_000,
                max_output_tokens=8192,
            ),
        )

        model_key = f"{provider.provider_id}/{endpoint.endpoint_id}"
        assert model_key not in {model.key for model in app._models}

        await pilot.press("slash", *"model", "space")
        await pilot.pause()

        popup = app.query_one(CommandPopup)
        assert popup.argument_command is not None
        assert popup.argument_command.name == "/model"
        assert popup.selected_argument is not None
        assert popup.selected_argument.value == model_key
        assert app._system_status.model_names == ("Hot Model",)
        assert app._system_status.model_count == 1

        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one(Composer).text == ""
        assert app._active_model == "Hot Model"


@pytest.mark.asyncio
async def test_model_command_shows_endpoint_and_provider_without_changing_route_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))
    selected: list[str] = []

    async def select_model(selector: str) -> AvailableModel:
        selected.append(selector)
        return app._models[0]

    async def models() -> tuple[AvailableModel, ...]:
        return app._models

    async with run_ready_app(app, size=(100, 32)) as pilot:
        await pilot.pause()
        model = AvailableModel(
            key="supplier/model-endpoint",
            provider_id="supplier",
            provider_display_name="Supplier",
            endpoint_id="model-endpoint",
            model_id="provider-model-id",
            display_name="Model display",
        )
        app._models = (
            model,
            model.model_copy(
                update={
                    "key": "other/gpt-20",
                    "provider_id": "other",
                    "provider_display_name": "Other",
                    "endpoint_id": "gpt-20",
                    "model_id": "gpt-20",
                    "display_name": "GPT-20",
                }
            ),
            model.model_copy(
                update={
                    "key": "supplier/gpt-11",
                    "endpoint_id": "gpt-11",
                    "model_id": "lower-id",
                    "display_name": "GPT-11",
                }
            ),
            model.model_copy(
                update={
                    "key": "supplier/gpt-9",
                    "endpoint_id": "gpt-9",
                    "model_id": "gpt-9",
                    "display_name": None,
                }
            ),
            model.model_copy(
                update={
                    "key": "supplier/gpt-10",
                    "endpoint_id": "gpt-10",
                    "model_id": "gpt-10",
                    "display_name": "",
                }
            ),
            model.model_copy(
                update={
                    "key": "supplier/gpt-8",
                    "endpoint_id": "gpt-8",
                    "model_id": "gpt-8",
                    "display_name": "中文模型",
                }
            ),
        )
        monkeypatch.setattr(app._controller, "models", models)
        monkeypatch.setattr(app._controller, "select_model", select_model)
        await pilot.press("slash", *"model", "space")
        await pilot.pause()

        composer = app.query_one(Composer)
        popup = app.query_one(CommandPopup)
        expected_order = [
            "other/gpt-20",
            "supplier/model-endpoint",
            "supplier/gpt-11",
            "supplier/gpt-10",
            "supplier/gpt-9",
            "supplier/gpt-8",
        ]
        assert popup.argument_command is not None
        assert [item.value for item in popup.argument_command.argument_options] == expected_order
        assert popup.argument_command.argument_options[5].display_label == "中文模型"
        choices = model_choices(app._models)
        assert [choice.value for choice in choices] == expected_order
        assert choices[5].label == "中文模型"
        assert choices[3].label == "gpt-10"
        assert choices[3].group == "Supplier"

        await pilot.press(*"model")
        await pilot.pause()
        argument = popup.selected_argument
        assert argument is not None
        assert argument.value == "supplier/model-endpoint"
        assert argument.display_label == "Model display"
        assert argument.description == "Supplier"
        assert composer.suggestion == ""

        content = popup._content
        rendered = command_popup_text(popup).plain.splitlines()[0]
        assert rendered.startswith("› Model display  Supplier")
        assert rendered.rindex("Supplier") < content.content_region.width - len("Supplier")

        await pilot.press("enter")
        await pilot.pause()
        assert composer.text == ""
        assert selected == ["supplier/model-endpoint"]
