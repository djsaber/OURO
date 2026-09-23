import pytest

from ouro.application.configuration import ConfigurationFacade
from ouro.core.errors import ConfigError
from ouro.tui.app import OuroTuiApp
from ouro.tui.screens import ChoiceScreen
from ouro.tui.theme import theme_for
from tests.tui.helpers import run_ready_app, two_route_application


@pytest.mark.parametrize("scope", ["all", "provider", "default"])
@pytest.mark.parametrize("selection", ["draft", "session"])
async def test_endpoint_menus_project_global_default_independently_of_session_selection(
    tmp_path, scope, selection
):
    configuration = ConfigurationFacade(tmp_path)
    provider_ids = {}
    endpoint_ids = {}
    for provider_name in ("gateway", "other"):
        created = configuration.add_provider(
            display_name=provider_name.title(),
            provider_type="custom",
            protocol="openai-chat-completions",
            base_url=f"https://{provider_name}.test",
        )
        provider_id = created.changed_provider_id
        assert provider_id is not None
        provider_ids[provider_name] = provider_id
        for endpoint_name in ("default", "recent"):
            endpoints = configuration.add_endpoint(
                provider_id,
                display_name=endpoint_name.title(),
                model=endpoint_name,
            )
            endpoint_id = endpoints.changed_endpoint_id
            assert endpoint_id is not None
            endpoint_ids[(provider_name, endpoint_name)] = endpoint_id
    gateway_id = provider_ids["gateway"]
    default_id = endpoint_ids[("gateway", "default")]
    recent_id = endpoint_ids[("gateway", "recent")]
    configuration.use_provider(gateway_id)
    application = two_route_application(
        tmp_path,
        provider_id=gateway_id,
        default_endpoint_id=default_id,
        recent_endpoint_id=recent_id,
    )
    recent_key = f"{gateway_id}/{recent_id}"
    default_key = f"{gateway_id}/{default_id}"
    session_id = (
        application.runtime.sessions.create(current_model=recent_key).session.session_id
        if selection == "session"
        else None
    )
    app = OuroTuiApp(application=application, resume_session_id=session_id)

    async with run_ready_app(app) as pilot:
        if selection == "draft":
            await app._select_model(recent_key)
        menu = ChoiceScreen(title="ACTIONS", placeholder="Choose an action…", choices=())
        app.push_screen(menu)
        await pilot.pause()
        if scope == "provider":
            await app._choose_endpoints(configuration.provider(gateway_id), menu=menu)
        else:
            await app._choose_endpoint_action("use" if scope == "default" else "list", menu=menu)
        await pilot.pause()
        choices = {choice.value: choice for choice in menu._choices}
        recent_choice = recent_id if scope == "provider" else recent_key
        default_choice = default_id if scope == "provider" else default_key
        if scope == "provider":
            assert [choice.value for choice in menu._choices] == [
                recent_id,
                default_id,
                "add-endpoint",
            ]
        assert choices[recent_choice].marker == ""
        assert choices[recent_choice].trailing == ""
        assert choices[default_choice].marker == theme_for(app).selected_choice_marker
        assert choices[default_choice].trailing == "default"
        if scope != "provider":
            other_id = provider_ids["other"]
            other_recent = f"{other_id}/{endpoint_ids[('other', 'recent')]}"
            other_default = f"{other_id}/{endpoint_ids[('other', 'default')]}"
            assert [choice.value for choice in menu._choices] == [
                recent_key,
                default_key,
                other_recent,
                other_default,
            ]
            assert choices[other_recent].marker == ""
            assert choices[other_default].marker == ""
            assert choices[other_recent].trailing == ""
            assert choices[other_default].trailing == ""

        if scope == "default":
            await menu._select_choice(recent_key)
        else:
            await app._controller.use_endpoint(gateway_id, recent_id)
            assert await menu.refresh_choices()
        choices = {choice.value: choice for choice in menu._choices}
        assert choices[recent_choice].marker == theme_for(app).selected_choice_marker
        assert choices[recent_choice].trailing == "default"
        assert choices[default_choice].marker == ""
        assert choices[default_choice].trailing == ""

        await app._select_model(default_key)
        assert await menu.refresh_choices()
        choices = {choice.value: choice for choice in menu._choices}
        assert choices[default_choice].marker == ""
        assert choices[default_choice].trailing == ""
        assert choices[recent_choice].marker == theme_for(app).selected_choice_marker
        assert choices[recent_choice].trailing == "default"


@pytest.mark.parametrize("selected", [None, "deleted/endpoint"])
async def test_endpoint_menu_remains_available_without_a_resolvable_selection(
    tmp_path, monkeypatch, selected
):
    configuration = ConfigurationFacade(tmp_path)
    created = configuration.add_provider(
        display_name="Gateway",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://gateway.test",
    )
    provider_id = created.changed_provider_id
    assert provider_id is not None
    endpoints = configuration.add_endpoint(
        provider_id,
        display_name="Default",
        model="default-model",
    )
    endpoint_id = endpoints.changed_endpoint_id
    assert endpoint_id is not None
    application = two_route_application(
        tmp_path,
        provider_id=provider_id,
        default_endpoint_id=endpoint_id,
    )
    session_id = (
        application.runtime.sessions.create(current_model=selected).session.session_id
        if selected is not None
        else None
    )
    app = OuroTuiApp(application=application, resume_session_id=session_id)

    async with run_ready_app(app) as pilot:

        def unavailable():
            raise ConfigError("default model is unavailable")

        monkeypatch.setattr(application.runtime, "current_model", unavailable)
        menu = ChoiceScreen(title="ACTIONS", placeholder="Choose an action…", choices=())
        app.push_screen(menu)
        await pilot.pause()
        await app._choose_endpoint_action("list", menu=menu)
        assert len(menu._choices) == 1
        assert menu._choices[0].value == f"{provider_id}/{endpoint_id}"
        assert menu._choices[0].marker == ""
        assert menu._choices[0].trailing == ""
        assert endpoint_id not in menu._choices[0].trailing
