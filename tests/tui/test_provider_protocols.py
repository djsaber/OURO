from __future__ import annotations

import json
from dataclasses import replace

import pytest
from textual.widgets import OptionList

from ouro.application.configuration.facade import ConfigurationFacade
from ouro.config import EndpointReasoningConfig, reasoning_effort_preset, reasoning_summary_preset
from ouro.core.errors import ConfigError
from ouro.tui.app import OuroTuiApp
from ouro.tui.features.providers.screens import (
    CUSTOM_PROVIDER_PROFILES,
    EndpointConfigurationScreen,
    ProviderConfigurationScreen,
    ProviderSetupProfile,
    profile_for_provider,
)
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.editable import EditableChoiceScreen
from tests.tui.helpers import (
    choose_menu,
    run_ready_app,
    two_route_application,
    wait_for_screen,
    wait_until,
)


async def test_endpoint_media_toggles_use_shared_configuration_validation(tmp_path):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    facade = ConfigurationFacade(tmp_path)
    provider_id = facade.add_provider(
        display_name="Media",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://media.test",
    ).changed_provider_id
    async with run_ready_app(app) as pilot:
        screen = EndpointConfigurationScreen(facade.provider(provider_id))
        app.push_screen(screen)
        await pilot.pause()
        choices = {choice.value: choice for choice in screen._choices}
        for kind, noun in (("image", "images"), ("audio", "audio"), ("video", "video")):
            choice = choices[f"endpoint-{kind}-input"]
            assert choice.label == f"{kind.title()} input"
            assert choice.description == f"Accept {noun} as model input."
        await screen._save_field("endpoint-model-id", "av")
        await select_choice(app, pilot, "endpoint-audio-input")
        await select_choice(app, pilot, "endpoint-video-input")
        saved = await app._controller.add_endpoint(provider_id, screen._draft())
        assert (
            saved.supports_audio_input
            and saved.supports_video_input
            and not saved.supports_image_input
        )
        editor = EndpointConfigurationScreen(facade.provider(provider_id), saved)
        assert editor._draft().supports_audio_input and editor._draft().supports_video_input

        responses_id = facade.add_provider(
            display_name="Images",
            provider_type="custom",
            protocol="openai-responses",
            base_url="https://images.test",
        ).changed_provider_id
        other = EndpointConfigurationScreen(facade.provider(responses_id))
        other._values["endpoint-model-id"] = "image-only"
        other._capabilities["endpoint-audio-input"] = True
        assert "does not support audio input" in other._validation_error()


async def test_header_editor_preserves_secrets_and_explicitly_replaces_or_clears(tmp_path):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    facade = ConfigurationFacade(tmp_path)
    created = facade.add_provider(
        display_name="Gateway",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://model.test",
        headers={"X-Secret": {"source": "literal", "value": "private-value"}},
    )
    async with run_ready_app(app, size=(100, 34)) as pilot:
        provider_id = created.changed_provider_id
        assert provider_id is not None
        provider = facade.provider(provider_id)
        profile = profile_for_provider(provider, await app._controller.provider_templates())
        screen = ProviderConfigurationScreen(profile, provider)
        app.push_screen(screen)
        await pilot.pause()
        assert screen._values["provider-headers"] == ""
        assert screen._draft().headers is None
        await app._controller.update_provider(provider_id, replace(screen._draft(), timeout=17))
        from ouro.config import ProviderConfigManager

        configs = ProviderConfigManager(tmp_path)
        configured = configs.get_provider(provider_id)
        assert configured is not None
        assert configured.headers["x-secret"].value == "private-value"
        await select_choice(app, pilot, "provider-headers")
        text = '{"X-Model":{"source":"context","key":"model.id"}}'
        await pilot.press(*text, "enter")
        await pilot.pause()
        await app._controller.update_provider(provider_id, screen._draft())
        configured = configs.get_provider(provider_id)
        assert configured is not None
        assert set(configured.headers) == {"x-model"}
        await screen._save_field("provider-headers", "{}")
        await app._controller.update_provider(provider_id, screen._draft())
        configured = configs.get_provider(provider_id)
        assert configured is not None and configured.headers == {}
        await screen._save_field("provider-headers", '{"X":{"source":"context","key":"unknown"}}')
        assert screen._validation_error()


async def select_choice(app, pilot, value):
    options = app.screen.query_one("#choice-list", OptionList)
    options.highlighted = options.get_option_index(value)
    options.focus()
    await pilot.press("enter")
    await pilot.pause()


@pytest.mark.parametrize(
    ("protocol", "preset", "summary_preset", "summaries"),
    [
        (
            "openai-responses",
            "openai-responses-effort",
            "openai-responses",
            ("auto", "concise", "detailed"),
        ),
        (
            "anthropic-messages",
            "anthropic-messages-adaptive-effort",
            "anthropic-messages",
            ("summarized", "omitted"),
        ),
    ],
)
async def test_endpoint_reasoning_presets_sync_settings_and_can_be_saved(
    tmp_path, protocol, preset, summary_preset, summaries
):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    facade = ConfigurationFacade(tmp_path)
    created = facade.add_provider(
        display_name="Gateway",
        provider_type="custom",
        protocol=protocol,
        base_url="https://gateway.test/v1",
    )
    provider_id = created.changed_provider_id
    assert provider_id is not None
    async with run_ready_app(app, size=(100, 34)) as pilot:
        screen = EndpointConfigurationScreen(facade.provider(provider_id))
        app.push_screen(screen)
        await pilot.pause()
        await screen._save_field("endpoint-model-id", "custom-model")
        choices = {choice.value: choice for choice in screen._choices}
        assert choices["endpoint-reasoning-effort"].label == "Effort"
        assert choices["endpoint-reasoning-summary"].label == "Summary"
        assert choices["endpoint-reasoning-config"].label == "Settings (JSON)"
        assert not choices["endpoint-reasoning-summary"].disabled
        await select_choice(app, pilot, "endpoint-reasoning-summary")
        options = app.screen.query_one("#choice-list", OptionList)
        assert not options.get_option(f"preset:{summary_preset}").disabled
        incompatible = (
            "anthropic-messages" if protocol == "openai-responses" else "openai-responses"
        )
        assert options.get_option(f"preset:{incompatible}").disabled
        await select_choice(app, pilot, f"preset:{summary_preset}")
        assert screen._draft().reasoning.capability.supported_summaries == summaries
        assert screen._draft().reasoning.capability.default.summary == summaries[0]
        await select_choice(app, pilot, "endpoint-reasoning-effort")
        options = app.screen.query_one("#choice-list", OptionList)
        assert not options.get_option(preset).disabled
        if protocol == "openai-responses":
            assert options.get_option("openai-chat-completions-effort").disabled
        await select_choice(app, pilot, preset)
        assert app.screen is screen
        assert screen._reasoning_effort_preset_name() == preset
        assert screen._draft().reasoning.capability.supported_summaries == summaries
        await select_choice(app, pilot, "endpoint-reasoning-summary")
        assert all(choice.group == "PRESETS" for choice in app.screen._choices)
        assert all(choice.value.startswith("preset:") for choice in app.screen._choices)
        await select_choice(app, pilot, f"preset:{summary_preset}")
        assert screen._draft().reasoning.capability.default.summary == summaries[0]
        await screen._save_field("endpoint-display-name", "Main")
        expected = reasoning_effort_preset(preset)
        expected.capability.supported_summaries = summaries
        expected.capability.default.summary = summaries[0]
        expected.profile.summary = reasoning_summary_preset(summary_preset).profile.summary
        settings = next(c for c in screen._choices if c.value == "endpoint-reasoning-config")
        assert settings.trailing == settings.edit_value
        assert EndpointReasoningConfig.model_validate_json(settings.edit_value) == expected
        await select_choice(app, pilot, "endpoint-reasoning-config")
        assert json.loads(screen._edit_text) == expected.model_dump(mode="json", exclude_none=True)
        await pilot.press("escape")

        await select_choice(app, pilot, "endpoint-reasoning-summary")
        await select_choice(app, pilot, "preset:none")
        cleared = screen._draft().reasoning
        assert cleared.capability.supported_summaries == ()
        assert cleared.capability.default.summary is None
        assert cleared.capability.default.effort == expected.capability.default.effort
        assert cleared.profile.effort == expected.profile.effort
        assert cleared.profile.summary == {}
        assert screen._reasoning_summary_label() == "None"

        expected.capability.default.effort = (
            "medium" if expected.capability.default.effort == "high" else "high"
        )
        expected.capability.default.summary = summaries[-1]
        screen._replace_choices(
            await screen._save_field("endpoint-reasoning-config", expected.model_dump_json()),
            selected="endpoint-reasoning-config",
        )
        assert screen._reasoning_effort_preset_name() is None
        assert screen._reasoning_summary_label() == ", ".join(value.title() for value in summaries)
        await select_choice(app, pilot, "endpoint-reasoning-summary")
        await select_choice(app, pilot, f"preset:{summary_preset}")
        assert screen._draft().reasoning.capability.default.summary == summaries[-1]
        saved = await app._controller.add_endpoint(provider_id, screen._draft())
        assert saved.reasoning == expected
        editor = EndpointConfigurationScreen(facade.provider(provider_id), saved)
        assert editor._reasoning_summary_label() == screen._reasoning_summary_label()
        assert editor._draft().reasoning is None


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
        await wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "+ provider", title="ADD PROVIDER")
        await select_choice(app, pilot, f"custom:{protocol}")
        await wait_for_screen(app, ProviderConfigurationScreen, title="ADD PROVIDER")
        screen = app.screen
        assert isinstance(screen, EditableChoiceScreen)
        assert all(choice.value != "provider-protocol" for choice in screen._choices)
        assert screen._values["provider-api-path"] == api_path
        for field, value in (
            ("provider-display-name", "Gateway"),
            ("provider-base-url", "https://gateway.test/v1"),
        ):
            await select_choice(app, pilot, field)
            await pilot.press("ctrl+u", *value, "enter")
            await pilot.pause()
        await select_choice(app, pilot, "provider-credential")
        await pilot.press("tab")
        await pilot.press(*"env:GATEWAY_KEY", "enter")
        await pilot.pause()
        await select_choice(app, pilot, "provider-save")
        facade = ConfigurationFacade(tmp_path)
        await wait_until(
            lambda: len(facade.provider_catalog().providers) == 1,
            pilot,
            description="saved custom provider",
        )
        providers = facade.provider_catalog().providers
        assert len(providers) == 1
        provider = providers[0]
        provider_id = provider.provider_id
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
        updated = await app._controller.update_provider(provider_id, draft)
        assert updated.protocol == protocol
        assert updated.api_path == api_path
        assert updated.timeout == 15
        before = facade.providers.config_path.read_bytes()
        with pytest.raises(ConfigError, match="protocol cannot be changed"):
            await app._controller.update_provider(
                provider_id,
                replace(
                    draft,
                    protocol=(
                        "anthropic-messages"
                        if protocol != "anthropic-messages"
                        else "openai-chat-completions"
                    ),
                ),
            )
        assert facade.providers.config_path.read_bytes() == before


@pytest.mark.parametrize("api_path", ["/chat/completions", "/custom/path", ""])
async def test_custom_provider_path_edit_keeps_the_chosen_protocol(tmp_path, api_path):
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
        assert all(choice.value != "provider-protocol" for choice in screen._choices)
        await select_choice(app, pilot, "provider-api-path")
        await pilot.press("ctrl+u", *"/gateway/chat", "enter")
        await pilot.pause()
        assert screen._draft().protocol == profile.protocol
        assert screen._draft().api_path == "/gateway/chat"


@pytest.mark.parametrize("provider_type", ["openai", "openai-codex", "anthropic"])
def test_template_protocol_is_visible_and_fixed(tmp_path, provider_type):
    template = ConfigurationFacade(tmp_path).provider_template(provider_type)
    screen = ProviderConfigurationScreen(ProviderSetupProfile.from_template(template))
    choice = next(c for c in screen._choices if c.value == "provider-protocol")
    assert choice.disabled
    assert screen._draft().protocol == template.protocol
    assert screen._draft().auth_driver == template.auth_driver
    assert screen._draft().api_key_env is None


def test_static_preset_credentials_remain_unbound_until_explicitly_configured(tmp_path):
    facade = ConfigurationFacade(tmp_path)
    template = facade.provider_template("deepseek")
    profile = ProviderSetupProfile.from_template(template)
    screen = ProviderConfigurationScreen(profile)
    credential = next(choice for choice in screen._choices if choice.value == "provider-credential")
    assert credential.trailing == ""
    assert screen._draft().api_key is None and screen._draft().api_key_env is None
    created = facade.add_provider(display_name="DeepSeek", provider_type="deepseek")
    provider = facade.provider(created.changed_provider_id)
    assert provider.credential.reference == "none"
    reopened = ProviderConfigurationScreen(profile, provider)
    assert reopened._draft().api_key_env is None


@pytest.mark.parametrize("profile", CUSTOM_PROVIDER_PROFILES)
def test_custom_connection_profile_selects_its_protocol_and_default_path(profile):
    screen = ProviderConfigurationScreen(replace(profile, base_url="https://gateway.test"))
    assert all(choice.value != "provider-protocol" for choice in screen._choices)
    assert screen._draft().protocol == profile.protocol
    assert screen._draft().api_path == profile.api_path
