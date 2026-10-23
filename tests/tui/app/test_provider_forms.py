"""TUI application scenarios: provider forms."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Input, OptionList, Static

from ouro.application.configuration.facade import ConfigurationFacade
from ouro.config import reasoning_effort_preset
from ouro.local_application import LocalApplication
from ouro.tui.app import OuroTuiApp
from ouro.tui.composer.input import Composer
from ouro.tui.features.providers.screens import (
    EndpointConfigurationScreen,
    ProviderConfigurationScreen,
    ProviderSetupProfile,
)
from ouro.tui.menus.choice import ChoiceScreen
from ouro.tui.menus.editable import EditableChoiceScreen
from ouro.tui.view_models import TuiEndpointConfiguration, TuiProviderConfiguration
from ouro.tui.widgets.activity import ActivityIndicator
from tests.tui.app.support import _application
from tests.tui.helpers import choose_menu, run_ready_app
from tests.tui.helpers import wait_for_screen as _wait_for_screen
from tests.tui.helpers import wait_until as _wait_until


@pytest.mark.asyncio
async def test_endpoint_configuration_screen_is_provider_scoped_and_validates_draft(
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
    provider = configuration.provider(provider_id)
    app = OuroTuiApp(application=_application(tmp_path))
    results: list[TuiEndpointConfiguration | None] = []

    async with run_ready_app(app, size=(80, 24)) as pilot:
        await pilot.pause()
        screen = EndpointConfigurationScreen(provider)
        app.push_screen(screen, callback=results.append)
        await pilot.pause()

        assert isinstance(screen, EditableChoiceScreen)
        assert not list(screen.query("#model-provider"))
        assert not list(screen.query("Button"))
        assert not list(screen.query("Checkbox"))
        assert screen.query_one("#choice-filter", Input).placeholder == (
            "Filter endpoint settings…"
        )
        assert [choice.group for choice in screen._choices] == [
            "DETAILS",
            "DETAILS",
            "LIMITS",
            "LIMITS",
            "CAPABILITIES",
            "CAPABILITIES",
            "CAPABILITIES",
            "CAPABILITIES",
            "CAPABILITIES",
            "CAPABILITIES",
            "THINKING",
            "THINKING",
            "THINKING",
            "REQUEST",
            "ACTIONS",
        ]
        assert screen._capabilities["endpoint-tools"] is True
        assert screen._capabilities["endpoint-audio-input"] is False
        assert screen._capabilities["endpoint-video-input"] is False
        assert screen._capabilities["endpoint-structured-output"] is True
        assert screen._values["endpoint-context-window"] == "1000000"
        assert screen._values["endpoint-max-output"] == ""

        options = screen.query_one("#choice-list", OptionList)
        for field_id, value in (
            ("endpoint-display-name", "New endpoint"),
            ("endpoint-model-id", "provider-model"),
            ("endpoint-context-window", "100"),
            ("endpoint-max-output", "200"),
        ):
            options.highlighted = options.get_option_index(field_id)
            options.focus()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("ctrl+u", *value, "enter")
            await pilot.pause()

        assert results == []
        assert options.get_option("endpoint-save").disabled is True
        save_choice = next(choice for choice in screen._choices if choice.value == "endpoint-save")
        assert "must be lower than" in save_choice.trailing

        options.highlighted = options.get_option_index("endpoint-max-output")
        options.focus()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("ctrl+u", *"50", "enter")
        await pilot.pause()

        options.highlighted = options.get_option_index("endpoint-image-input")
        options.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert screen._capabilities["endpoint-image-input"] is True
        assert screen._capabilities["endpoint-streaming"] is True

        for invalid, error in (
            ("[]", "JSON object"),
            ('{"capability":{"default_effort":null}}', "run ouro-upgrade"),
        ):
            screen._values["endpoint-reasoning-config"] = invalid
            screen._choices = screen._endpoint_choices()
            screen._refresh_options(needle="", preferred_value="endpoint-reasoning-config")
            assert options.get_option("endpoint-save").disabled is True
            save_choice = next(
                choice for choice in screen._choices if choice.value == "endpoint-save"
            )
            assert error in save_choice.trailing

        options.highlighted = options.get_option_index("endpoint-reasoning-effort")
        options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="EFFORT")
        preset_options = app.screen.query_one("#choice-list", OptionList)
        assert preset_options.get_option("anthropic-messages-adaptive-effort").disabled is False
        adaptive = next(
            choice
            for choice in app.screen._choices
            if choice.value == "anthropic-messages-adaptive-effort"
        )
        assert "5 efforts" in adaptive.trailing
        preset_options.highlighted = preset_options.get_option_index("thinking-toggle")
        preset_options.focus()
        await pilot.press("enter")
        await _wait_for_screen(app, EndpointConfigurationScreen, title="ADD ENDPOINT")
        assert screen._reasoning_effort_preset_name() == "thinking-toggle"
        assert screen._draft().reasoning == reasoning_effort_preset("thinking-toggle")
        assert options.get_option("endpoint-save").disabled is False

        screen._values["endpoint-reasoning-config"] = (
            '{"capability":{"activation":"toggle","default":{"mode":"enabled"}},'
            '"profile":{"name":"vendor-thinking","effort":'
            '{"enabled":{"thinking":true},"disabled":{"thinking":false}}}}'
        )
        screen._choices = screen._endpoint_choices()
        screen._refresh_options(needle="", preferred_value="endpoint-reasoning-config")
        assert screen._reasoning_effort_preset_name() is None
        assert options.get_option("endpoint-save").disabled is False

        screen._values["endpoint-request-options"] = '{"thinking":true}'
        screen._choices = screen._endpoint_choices()
        screen._refresh_options(needle="", preferred_value="endpoint-request-options")
        assert options.get_option("endpoint-save").disabled is True
        save_choice = next(choice for choice in screen._choices if choice.value == "endpoint-save")
        assert "reasoning-owned fields: thinking" in save_choice.trailing

        screen._values["endpoint-request-options"] = "[]"
        screen._choices = screen._endpoint_choices()
        screen._refresh_options(needle="", preferred_value="endpoint-request-options")
        assert options.get_option("endpoint-save").disabled is True
        save_choice = next(choice for choice in screen._choices if choice.value == "endpoint-save")
        assert "JSON object" in save_choice.trailing

        screen._values["endpoint-request-options"] = '{"seed":7}'
        screen._choices = screen._endpoint_choices()
        screen._refresh_options(needle="", preferred_value="endpoint-save")
        assert options.get_option("endpoint-save").disabled is False

        options.highlighted = options.get_option_index("endpoint-save")
        await pilot.press("enter")
        await pilot.pause()

        assert len(results) == 1
        draft = results[0]
        assert draft is not None
        assert draft.display_name == "New endpoint"
        assert draft.model_id == "provider-model"
        assert draft.context_window == 100
        assert draft.max_output_tokens == 50
        assert draft.supports_tools is True
        assert draft.supports_image_input is True
        assert draft.supports_audio_input is False
        assert draft.supports_video_input is False
        assert draft.supports_streaming is True
        assert draft.supports_structured_output is True
        assert draft.reasoning is not None
        assert draft.reasoning.profile is not None
        assert draft.reasoning.profile.name == "vendor-thinking"
        assert draft.request_options == {"seed": 7}

        saved = await app._controller.add_endpoint(provider_id, draft)
        edit = EndpointConfigurationScreen(provider, saved)
        app.push_screen(edit)
        await pilot.pause()
        edit._refresh_options(needle="", preferred_value="endpoint-max-output")
        edit.query_one("#choice-list", OptionList).focus()
        await pilot.press("enter", "ctrl+u", "enter")
        await pilot.pause()
        cleared = await app._controller.update_endpoint(
            provider_id, saved.endpoint_id, edit._draft()
        )
        assert cleared.max_output_tokens is None
        assert cleared.reasoning == saved.reasoning
        assert cleared.request_options == saved.request_options
        reopened = EndpointConfigurationScreen(
            provider, configuration.endpoint(provider_id, saved.endpoint_id)
        )
        output_choice = next(c for c in reopened._choices if c.value == "endpoint-max-output")
        assert output_choice.label == "Max output tokens"
        assert output_choice.trailing == "Optional"
        assert output_choice.edit_placeholder == ""


@pytest.mark.asyncio
async def test_actions_opens_provider_scoped_endpoint_configuration(tmp_path: Path) -> None:
    configuration = ConfigurationFacade(tmp_path)
    configuration.add_provider(
        display_name="Zulu",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://zulu.example.invalid/v1",
    )
    configuration.add_provider(
        display_name="Custom",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://example.invalid/v1",
    )
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "+ endpoint", title="ADD ENDPOINT • PROVIDER")
        assert app.screen.query_one("#choice-title", Static).render().plain == (
            "ADD ENDPOINT • PROVIDER"
        )
        assert [choice.label for choice in app.screen._choices] == ["Custom", "Zulu"]

        await pilot.press("enter")
        await _wait_for_screen(app, EndpointConfigurationScreen, title="ADD ENDPOINT")

        assert isinstance(app.screen, EndpointConfigurationScreen)
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_empty_model_catalog_opens_provider_setup_without_sending(tmp_path: Path) -> None:
    app = OuroTuiApp(application=LocalApplication.build(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.pause()
        assert "faux" not in app.query_one(ActivityIndicator).render().plain.casefold()
        composer = app.query_one(Composer)
        composer.load_text("Keep this draft")
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="ADD PROVIDER")

        assert app._controller.session_id is None
        assert composer.text == "Keep this draft"


@pytest.mark.asyncio
async def test_add_provider_flow_integrates_presets_and_custom_profiles(
    tmp_path: Path,
) -> None:
    app = OuroTuiApp(application=_application(tmp_path))

    async with run_ready_app(app, size=(100, 34)) as pilot:
        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        await choose_menu(pilot, "+ provider", title="ADD PROVIDER")

        assert {"Ready to use", "Custom connection"} == {
            choice.group for choice in app.screen._choices
        }
        assert {
            "Google Gemini",
            "OpenAI Chat Completions",
            "OpenAI Responses",
            "Anthropic Messages",
        } <= {choice.label for choice in app.screen._choices}
        ready_labels = [
            choice.label for choice in app.screen._choices if choice.group == "Ready to use"
        ]
        custom_labels = [
            choice.label for choice in app.screen._choices if choice.group == "Custom connection"
        ]
        assert ready_labels == sorted(ready_labels, key=str.casefold)
        assert custom_labels == [
            "Anthropic Messages",
            "OpenAI Chat Completions",
            "OpenAI Responses",
        ]

        app.screen.query_one("#choice-filter", Input).value = "Google Gemini"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ProviderConfigurationScreen, title="ADD PROVIDER")
        assert isinstance(app.screen, ProviderConfigurationScreen)
        assert not list(app.screen.query("#provider-default"))

        app.screen._values["provider-display-name"] = "Gemini Work"
        app.screen._choices = app.screen._provider_choices()
        app.screen._refresh_options(needle="", preferred_value="provider-save")
        app.screen.query_one("#choice-list", OptionList).focus()
        await pilot.press("enter")
        await _wait_until(
            lambda: any(
                provider.display_name == "Gemini Work"
                for provider in ConfigurationFacade(tmp_path).provider_catalog().providers
            ),
            pilot,
            description="saved provider",
        )
        provider = next(
            item
            for item in ConfigurationFacade(tmp_path).provider_catalog().providers
            if item.display_name == "Gemini Work"
        )
        assert provider.provider_type == "gemini"
        assert provider.endpoint_count == 3

        await pilot.press("ctrl+p")
        await _wait_for_screen(app, ChoiceScreen, title="ACTIONS")
        app.screen.query_one("#choice-filter", Input).value = "Manage providers"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="PROVIDERS")
        app.screen.query_one("#choice-filter", Input).value = "+"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ChoiceScreen, title="ADD PROVIDER")
        app.screen.query_one("#choice-filter", Input).value = "Anthropic Messages"
        await pilot.pause()
        await pilot.press("enter")
        await _wait_for_screen(app, ProviderConfigurationScreen, title="ADD PROVIDER")

        assert isinstance(app.screen, ProviderConfigurationScreen)
        assert app.screen._values["provider-base-url"] == ""
        assert app.screen._values["provider-api-path"] == "/v1/messages"
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_provider_configuration_uses_shared_grouped_inline_menu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ProviderSetupProfile(
        provider_type="custom",
        protocol="openai-chat-completions",
        display_name="Gateway",
        description="Test gateway",
        base_url="https://gateway.example.invalid/v1",
        api_path="/chat/completions",
        credential_env="GATEWAY_API_KEY",
        endpoint_count=0,
        preset=False,
    )
    app = OuroTuiApp(application=_application(tmp_path))
    results: list[TuiProviderConfiguration | None] = []

    async with run_ready_app(app, size=(80, 24)) as pilot:
        await pilot.pause()
        screen = ProviderConfigurationScreen(profile)
        app.push_screen(screen, callback=results.append)
        await pilot.pause()

        assert isinstance(screen, EditableChoiceScreen)
        assert not list(screen.query("Button"))
        assert not list(screen.query("Checkbox"))
        assert screen.query_one("#choice-filter", Input).placeholder == (
            "Filter provider settings…"
        )
        options = screen.query_one("#choice-list", OptionList)
        assert options.styles.scrollbar_size_vertical == 0
        assert [(choice.value, choice.group) for choice in screen._choices] == [
            ("provider-credential", "CREDENTIALS"),
            ("provider-display-name", "DETAILS"),
            ("provider-base-url", "ADVANCED"),
            ("provider-api-path", "ADVANCED"),
            ("provider-timeout", "ADVANCED"),
            ("provider-headers", "REQUEST"),
            ("provider-options", "REQUEST"),
            ("provider-save", "ACTIONS"),
        ]
        assert screen._values["provider-api-key-env"] == ""
        assert next(c for c in screen._choices if c.value == "provider-credential").trailing == ""
        assert screen._values["provider-base-url"] == "https://gateway.example.invalid/v1"
        assert screen._values["provider-api-path"] == "/chat/completions"
        assert screen._values["provider-timeout"] == "60.0"
        assert screen._values["provider-options"] == "{}"

        options.highlighted = options.get_option_index("provider-credential")
        options.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is screen
        assert screen._credential_source == "inline"
        await pilot.press(*"pre", "left")
        monkeypatch.setattr(
            "ouro.tui.menus.editable.read_system_clipboard",
            lambda: "pasted\non two lines",
        )
        await pilot.click(options, offset=(8, 1), button=3)
        await pilot.pause()
        assert screen._edit_text == "prpasted on two linese"
        assert "pasted" not in str(options.get_option("provider-credential").prompt)
        await pilot.press("enter")
        await pilot.pause()
        assert screen._values["provider-api-key"] == "prpasted on two linese"
        assert screen._values["provider-api-key-env"] == ""

        screen._values["provider-base-url"] = ""
        screen._choices = screen._provider_choices()
        screen._refresh_options(needle="", preferred_value="provider-base-url")
        assert options.get_option("provider-save").disabled is True

        screen._values["provider-base-url"] = "https://gateway.invalid/v1"
        screen._values["provider-options"] = "[]"
        screen._choices = screen._provider_choices()
        screen._refresh_options(needle="", preferred_value="provider-options")
        assert options.get_option("provider-save").disabled is True

        screen._values["provider-options"] = '{"tool_choice":"none","top_p":0.8}'
        screen._choices = screen._provider_choices()
        screen._refresh_options(needle="", preferred_value="provider-save")
        assert options.get_option("provider-save").disabled is False
        await pilot.press("enter")
        await pilot.pause()

        assert len(results) == 1
        draft = results[0]
        assert draft is not None
        assert draft.api_key == "prpasted on two linese"
        assert draft.api_key_env is None
        assert draft.enabled is True
        assert draft.provider_options == {"tool_choice": "none", "top_p": 0.8}


def test_provider_editors_preserve_redacted_request_options_when_unchanged(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    created = configuration.add_provider(
        display_name="Gateway",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://gateway.example.invalid/v1",
        provider_options={"api_key": "request-secret", "top_p": 0.8},
    )
    provider_id = created.changed_provider_id
    assert provider_id is not None
    endpoints = configuration.add_endpoint(
        provider_id,
        display_name="Chat",
        model="gateway-model",
        request_options={"nested": {"password": "endpoint-secret"}, "seed": 7},
    )
    endpoint_id = endpoints.changed_endpoint_id
    assert endpoint_id is not None
    provider = configuration.provider(provider_id)
    endpoint = configuration.endpoint(provider_id, endpoint_id)
    profile = ProviderSetupProfile(
        provider_type="custom",
        protocol="openai-chat-completions",
        display_name="Gateway",
        description="Test gateway",
        base_url="https://gateway.example.invalid/v1",
        api_path="/chat/completions",
        credential_env=None,
        endpoint_count=1,
        preset=False,
    )

    provider_draft = ProviderConfigurationScreen(profile, provider)._draft()
    endpoint_draft = EndpointConfigurationScreen(provider, endpoint)._draft()

    assert provider.provider_options == {"api_key": "***", "top_p": 0.8}
    assert endpoint.request_options == {"nested": {"password": "***"}, "seed": 7}
    assert provider_draft.provider_options is None
    assert endpoint_draft.reasoning is None
    assert endpoint_draft.request_options is None


@pytest.mark.asyncio
async def test_provider_configuration_preserves_existing_enabled_state(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    created = configuration.add_provider(
        display_name="Disabled Gateway",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://gateway.example.invalid/v1",
        enabled=False,
    )
    provider_id = created.changed_provider_id
    assert provider_id is not None
    profile = ProviderSetupProfile(
        provider_type="custom",
        protocol="openai-chat-completions",
        display_name="OpenAI Chat Completions",
        description="Test gateway",
        base_url="",
        api_path="/chat/completions",
        credential_env=None,
        endpoint_count=0,
        preset=False,
    )
    app = OuroTuiApp(application=_application(tmp_path))
    results: list[TuiProviderConfiguration | None] = []

    async with run_ready_app(app, size=(80, 24)) as pilot:
        await pilot.pause()
        app.push_screen(
            ProviderConfigurationScreen(
                profile,
                existing=configuration.provider(provider_id),
            ),
            callback=results.append,
        )
        await pilot.pause()

        actions = app.screen.query_one("#choice-list", OptionList)
        action_ids = [
            str(option.id)
            for index in range(actions.option_count)
            if (option := actions.get_option_at_index(index)).id is not None
            and str(option.id).startswith("provider-save")
        ]
        assert action_ids == ["provider-save"]
        actions.highlighted = actions.get_option_index("provider-save")
        actions.focus()
        await pilot.press("enter")
        await pilot.pause()

        assert len(results) == 1
        draft = results[0]
        assert draft is not None
        assert draft.enabled is False
