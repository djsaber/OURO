from __future__ import annotations

import pytest
from textual import events
from textual.widgets import OptionList, Static

from ouro.application.configuration import ConfigurationFacade
from ouro.tui.app import OuroTuiApp
from ouro.tui.provider_screens import (
    CUSTOM_PROVIDER_PROFILES,
    ProviderConfigurationScreen,
    profile_for_provider,
)
from ouro.tui.theme import theme_for
from tests.tui.helpers import run_ready_app, two_route_application
from tests.tui.test_provider_protocols import select_choice


@pytest.mark.parametrize("source", ("inline", "env"))
async def test_provider_credentials_preserve_replace_and_mask_both_sources(tmp_path, source):
    facade = ConfigurationFacade(tmp_path)
    created = facade.add_provider(
        display_name="Gateway",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://gateway.test",
        **({"api_key": "private-original"} if source == "inline" else {"api_key_env": "ORIGINAL"}),
    )
    provider_id = created.changed_provider_id
    provider = facade.provider(provider_id)
    profile = profile_for_provider(provider, facade.provider_template_catalog().templates)
    screen = ProviderConfigurationScreen(profile, provider)
    original = screen._draft()
    app = OuroTuiApp(application=two_route_application(tmp_path))
    async with run_ready_app(app) as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        choices = {choice.value: choice for choice in screen._choices}
        assert choices["provider-credential"].trailing
        assert choices["provider-credential"].edit_value == (
            "" if source == "inline" else "env:ORIGINAL"
        )

        async def edit_source(target):
            await select_choice(app, pilot, "provider-credential")
            assert app.screen is screen
            if screen._credential_source != target:
                await pilot.press("tab")
            prompt = screen.query_one(OptionList).get_option("provider-credential").prompt
            assert prompt.plain.endswith("[KEY]" if target == "inline" else "[ENV]")
            assert "private-original" not in prompt.plain

        # Explicitly submitting a blank provider credential keeps the saved binding.
        await edit_source(source)
        await pilot.press("ctrl+u", "enter")
        await pilot.pause()
        assert screen._draft() == original
        await app._controller.update_provider(provider_id, screen._draft())
        assert facade.provider(provider_id).credential.reference == provider.credential.reference

        await edit_source("env")
        await pilot.press("ctrl+u", *"env:", "enter")
        await pilot.pause()
        assert screen._draft() == original

        await edit_source("inline")
        await pilot.press(*"discarded", "escape")
        assert screen._draft() == original

        for target, value in (("env", "env:REPLACEMENT"), ("inline", " env:literal-key ")):
            await edit_source(target)
            await pilot.press("ctrl+u")
            app.screen.post_message(events.Paste(value))
            await pilot.pause()
            prompt = app.screen.query_one(OptionList).get_option("provider-credential").prompt
            assert value not in prompt.plain
            await pilot.press("enter")
            await pilot.pause()
            saved = await app._controller.update_provider(provider_id, screen._draft())
            assert saved.credential.reference == (
                "env:REPLACEMENT" if target == "env" else "inline:***"
            )
            stored = facade.providers.get_provider(provider_id).credential_ref
            assert (stored.name if target == "env" else stored.value) == (
                "REPLACEMENT" if target == "env" else value
            )
            row = next(c for c in screen._choices if c.value == "provider-credential")
            assert row.trailing.endswith("[KEY]" if target == "inline" else "[ENV]")


async def test_credential_tab_preserves_each_input_cancel_state_and_navigation(tmp_path):
    app = OuroTuiApp(application=two_route_application(tmp_path))
    screen = ProviderConfigurationScreen(CUSTOM_PROVIDER_PROFILES[0])
    navigation = []

    async def switch_menu():
        navigation.append("tab")

    screen._on_tab = switch_menu
    async with run_ready_app(app, size=(80, 30)) as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        stack_size = len(app.screen_stack)
        await pilot.press("tab")
        assert navigation == ["tab"]
        await select_choice(app, pilot, "provider-credential")
        options = screen.query_one(OptionList)
        assert "Tab type" in str(screen.query_one("#choice-hint", Static).content)
        assert screen._edit_text == "" and screen._editor_trailing() == "[KEY]"
        await pilot.press(*"local-key", "tab")
        assert screen._edit_text == "" and screen._editor_trailing() == "[ENV]"
        await pilot.press(*"env:LOCAL_KEY", "tab")
        assert screen._edit_text == "local-key" and screen._editor_trailing() == "[KEY]"
        await pilot.press("tab")
        assert screen._edit_text == "env:LOCAL_KEY"
        await pilot.press("escape")
        assert app.screen is screen and len(app.screen_stack) == stack_size
        assert screen._values["provider-api-key"] == screen._values["provider-api-key-env"] == ""
        assert screen._credential_buffers == {} and screen._credential_source is None
        assert next(c for c in screen._choices if c.value == "provider-credential").trailing == ""

        await select_choice(app, pilot, "provider-credential")
        await pilot.press("tab")
        screen.post_message(events.Paste("env:" + "WIDE_KEY_" * 20))
        await pilot.pause()
        for width in (40, 28, 80):
            await pilot.resize_terminal(width, 22)
            await pilot.pause()
            prompt = options.get_option("provider-credential").prompt
            assert prompt.plain.endswith("[ENV]")
            assert "WIDE_KEY" not in prompt.plain
            row_width = (
                options.scrollable_content_region.width
                - options.get_component_styles("option-list--option").padding.width
            )
            assert prompt.cell_len == row_width
            assert (
                options._get_visual(options.get_option("provider-credential")).get_height(
                    options.styles, row_width
                )
                == 1
            )
        await pilot.press("ctrl+u")
        assert options.get_option("provider-credential").prompt.plain.endswith("[ENV]")
        await pilot.press(*"env:FINAL_KEY", "enter")
        row = next(c for c in screen._choices if c.value == "provider-credential")
        assert row.trailing == theme_for(screen).mask_character * 5 + " [ENV]"
        assert navigation == ["tab"]
        await select_choice(app, pilot, "provider-display-name")
        await pilot.press("ctrl+u", *"Visible", "tab")
        assert options.get_option("provider-display-name").prompt.plain == "Visible "
        assert navigation == ["tab"]
        await pilot.press("escape", "tab")
        assert navigation == ["tab", "tab"]
