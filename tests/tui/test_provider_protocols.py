from __future__ import annotations

from dataclasses import replace

import pytest
from textual.widgets import Input, OptionList

from ouro.application.configuration import ConfigurationFacade
from ouro.config import reasoning_preset
from ouro.core.errors import ConfigError
from ouro.tui.app import OuroTuiApp
from ouro.tui.provider_screens import (
    CUSTOM_PROVIDER_PROFILES,
    EndpointConfigurationScreen,
    ProviderConfigurationScreen,
    ProviderSetupProfile,
    profile_for_provider,
)
from ouro.tui.screens import ChoiceScreen, EditableChoiceScreen
from tests.tui.helpers import run_ready_app, two_route_application


async def test_header_editor_preserves_secrets_and_explicitly_replaces_or_clears(tmp_path):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    facade = ConfigurationFacade(tmp_path)
    facade.add_provider(
        "gateway",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://model.test",
        headers={"X-Secret": {"source": "literal", "value": "private-value"}},
    )
    async with run_ready_app(app, size=(100, 34)) as pilot:
        provider = facade.provider("gateway")
        profile = profile_for_provider(provider, await app._controller.provider_templates())
        screen = ProviderConfigurationScreen(profile, provider)
        app.push_screen(screen)
        await pilot.pause()
        assert screen._values["provider-headers"] == ""
        assert screen._draft().headers is None
        await app._controller.update_provider(replace(screen._draft(), timeout=17))
        from ouro.config import ProviderConfigManager

        configs = ProviderConfigManager(tmp_path)
        assert configs.get_provider("gateway").headers["x-secret"].value == "private-value"
        await select_choice(app, pilot, "provider-headers")
        text = '{"X-Model":{"source":"context","key":"model.id"}}'
        await pilot.press(*text, "enter")
        await pilot.pause()
        await app._controller.update_provider(screen._draft())
        assert set(configs.get_provider("gateway").headers) == {"x-model"}
        await screen._save_field("provider-headers", "{}")
        await app._controller.update_provider(screen._draft())
        assert configs.get_provider("gateway").headers == {}
        await screen._save_field("provider-headers", '{"X":{"source":"context","key":"unknown"}}')
        assert screen._validation_error()


async def select_choice(app, pilot, value):
    options = app.screen.query_one("#choice-list", OptionList)
    options.highlighted = options.get_option_index(value)
    options.focus()
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.parametrize("preset", ["openai-responses-effort", "openai-responses-effort-summary"])
async def test_responses_endpoint_thinking_preset_can_be_selected_and_saved(tmp_path, preset):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    facade = ConfigurationFacade(tmp_path)
    facade.add_provider(
        "gateway",
        provider_type="custom",
        protocol="openai-responses",
        base_url="https://gateway.test/v1",
    )
    async with run_ready_app(app, size=(100, 34)) as pilot:
        screen = EndpointConfigurationScreen(facade.provider("gateway"))
        app.push_screen(screen)
        await pilot.pause()
        await select_choice(app, pilot, "endpoint-reasoning-preset")
        options = app.screen.query_one("#choice-list", OptionList)
        assert not options.get_option(preset).disabled
        assert options.get_option("openai-chat-completions-effort").disabled
        await select_choice(app, pilot, preset)
        assert app.screen is screen
        assert screen._reasoning_preset_name() == preset
        await screen._save_field("endpoint-id", "main")
        await screen._save_field("endpoint-model-id", "custom-model")
        await app._controller.add_endpoint(screen._draft())
        saved = facade.endpoint("gateway", "main")
        assert saved.reasoning == reasoning_preset(preset)
        assert saved.reasoning.capability.output == "summary"
        editor = EndpointConfigurationScreen(facade.provider("gateway"), saved)
        assert editor._reasoning_preset_name() == preset


@pytest.mark.parametrize(
    ("protocol", "label", "api_path"),
    [
        ("openai-chat-completions", "OpenAI Chat Completions", "/chat/completions"),
        ("openai-responses", "OpenAI Responses", "/responses"),
        ("anthropic-messages", "Anthropic Messages", "/v1/messages"),
    ],
)
async def test_create_custom_provider_through_protocol_entry_and_preserve_on_edit(
    tmp_path, protocol, label, api_path
):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        app.screen.query_one("#choice-filter", Input).value = "Add provider"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await select_choice(app, pilot, f"custom:{protocol}")
        screen = app.screen
        assert isinstance(screen, EditableChoiceScreen)
        assert screen._values["provider-api-path"] == api_path
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
        assert provider.provider_type == "custom"
        assert provider.protocol == protocol
        assert provider.api_path == api_path
        assert provider.auth_driver == "static"
        assert provider.credential.reference == "env:GATEWAY_KEY"
        assert provider.endpoint_count == 0

        profile = profile_for_provider(provider, await app._controller.provider_templates())
        assert profile.protocol == protocol
        editor = ProviderConfigurationScreen(profile, provider)
        protocol_choice = next(c for c in editor._choices if c.value == "provider-protocol")
        assert protocol_choice.disabled
        assert protocol_choice.trailing == label
        draft = replace(editor._draft(), timeout=15)
        updated = await app._controller.update_provider(draft)
        assert updated.protocol == protocol
        assert updated.api_path == api_path
        assert updated.timeout == 15
        before = facade.config_path.read_bytes()
        with pytest.raises(ConfigError, match="protocol cannot be changed"):
            await app._controller.update_provider(
                replace(
                    draft,
                    protocol=(
                        "anthropic-messages"
                        if protocol != "anthropic-messages"
                        else "openai-chat-completions"
                    ),
                )
            )
        assert facade.config_path.read_bytes() == before


@pytest.mark.parametrize("api_path", ["/chat/completions", "/custom/path", ""])
async def test_protocol_selection_cancellation_and_path_defaults(tmp_path, api_path):
    profile = replace(
        CUSTOM_PROVIDER_PROFILES[0],
        base_url="https://gateway.test/v1",
        api_path=api_path,
    )
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app, size=(100, 34)) as pilot:
        screen = ProviderConfigurationScreen(profile)
        app.push_screen(screen)
        await pilot.pause()
        await select_choice(app, pilot, "provider-protocol")
        assert isinstance(app.screen, ChoiceScreen)
        assert {choice.value for choice in app.screen._choices} == {
            "openai-chat-completions",
            "openai-responses",
            "anthropic-messages",
        }
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


@pytest.mark.parametrize("profile", CUSTOM_PROVIDER_PROFILES)
def test_custom_connection_profile_selects_its_protocol_and_default_path(profile):
    screen = ProviderConfigurationScreen(replace(profile, base_url="https://gateway.test"))
    choice = next(c for c in screen._choices if c.value == "provider-protocol")
    assert not choice.disabled
    assert screen._draft().protocol == profile.protocol
    assert screen._draft().api_path == profile.api_path
