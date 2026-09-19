from __future__ import annotations

from dataclasses import replace

import pytest
from textual.widgets import Input, OptionList

from ouro.application.configuration import ConfigurationFacade
from ouro.core.errors import ConfigError
from ouro.tui.app import OuroTuiApp
from ouro.tui.provider_screens import (
    COMPATIBLE_PROVIDER_PROFILES,
    ProviderConfigurationScreen,
    ProviderSetupProfile,
    profile_for_provider,
)
from ouro.tui.screens import ChoiceScreen, EditableChoiceScreen
from tests.tui.helpers import run_ready_app, two_route_application


async def select_choice(app, pilot, value):
    options = app.screen.query_one("#choice-list", OptionList)
    options.highlighted = options.get_option_index(value)
    options.focus()
    await pilot.press("enter")
    await pilot.pause()


async def test_create_responses_provider_through_shared_menu_and_preserve_on_edit(tmp_path):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        app.screen.query_one("#choice-filter", Input).value = "Add provider"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await select_choice(app, pilot, "compatible:openai-compatible")
        screen = app.screen
        assert isinstance(screen, EditableChoiceScreen)
        await select_choice(app, pilot, "provider-protocol")
        assert isinstance(app.screen, ChoiceScreen)
        assert {choice.value for choice in app.screen._choices} == {
            "openai-chat-completions",
            "openai-responses",
        }
        await select_choice(app, pilot, "openai-responses")
        assert app.screen is screen
        assert screen._values["provider-api-path"] == "/responses"
        for field, value in (
            ("provider-id", "gateway"),
            ("provider-base-url", "https://gateway.test/v1"),
            ("provider-api-key-env", "GATEWAY_KEY"),
        ):
            await select_choice(app, pilot, field)
            await pilot.press("ctrl+u", *value, "enter")
            await pilot.pause()
        await select_choice(app, pilot, "provider-save")
        facade = ConfigurationFacade(tmp_path)
        provider = facade.provider("gateway")
        assert provider.protocol == "openai-responses"
        assert provider.api_path == "/responses"
        assert provider.auth_driver == "static"
        assert provider.credential.reference == "env:GATEWAY_KEY"
        assert provider.endpoint_count == 0

        profile = profile_for_provider(provider, await app._controller.provider_templates())
        editor = ProviderConfigurationScreen(profile, provider)
        protocol_choice = next(c for c in editor._choices if c.value == "provider-protocol")
        assert protocol_choice.disabled
        assert protocol_choice.trailing == "OpenAI Responses"
        draft = replace(editor._draft(), timeout=15)
        updated = await app._controller.update_provider(draft)
        assert updated.protocol == "openai-responses"
        assert updated.api_path == "/responses"
        assert updated.timeout == 15
        before = facade.config_path.read_bytes()
        with pytest.raises(ConfigError, match="protocol cannot be changed"):
            await app._controller.update_provider(
                replace(draft, protocol="openai-chat-completions")
            )
        assert facade.config_path.read_bytes() == before


@pytest.mark.parametrize("api_path", ["/chat/completions", "/custom/path", ""])
async def test_protocol_selection_cancellation_and_path_defaults(tmp_path, api_path):
    profile = replace(
        COMPATIBLE_PROVIDER_PROFILES[0],
        base_url="https://gateway.test/v1",
        api_path=api_path,
    )
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app, size=(100, 34)) as pilot:
        screen = ProviderConfigurationScreen(profile)
        app.push_screen(screen)
        await pilot.pause()
        await select_choice(app, pilot, "provider-protocol")
        await pilot.press("escape")
        await pilot.pause()
        assert screen._draft().protocol == "openai-chat-completions"
        assert screen._values["provider-api-path"] == api_path
        await select_choice(app, pilot, "provider-protocol")
        await select_choice(app, pilot, "openai-responses")
        assert screen._draft().protocol == "openai-responses"
        assert screen._values["provider-api-path"] == (
            "/responses" if api_path == "/chat/completions" else api_path
        )
        await select_choice(app, pilot, "provider-protocol")
        await select_choice(app, pilot, "openai-chat-completions")
        assert screen._values["provider-api-path"] == api_path


@pytest.mark.parametrize("provider_type", ["openai", "openai-codex", "anthropic"])
def test_template_protocol_is_visible_and_fixed(tmp_path, provider_type):
    template = ConfigurationFacade(tmp_path).provider_template(provider_type)
    screen = ProviderConfigurationScreen(ProviderSetupProfile.from_template(template))
    choice = next(c for c in screen._choices if c.value == "provider-protocol")
    assert choice.disabled
    assert screen._draft().protocol == template.protocol
    assert screen._draft().auth_driver == template.auth_driver


def test_anthropic_custom_connection_has_fixed_messages_protocol():
    profile = replace(COMPATIBLE_PROVIDER_PROFILES[1], base_url="https://gateway.test")
    screen = ProviderConfigurationScreen(profile)
    choice = next(c for c in screen._choices if c.value == "provider-protocol")
    assert choice.disabled
    assert screen._draft().protocol == "anthropic-messages"
