import pytest

from ouro.application.configuration import ConfigurationFacade
from ouro.core.errors import ConfigError
from ouro.tui.app import OuroTuiApp
from ouro.tui.screens import ChoiceScreen
from ouro.tui.theme import theme_for
from tests.tui.helpers import run_ready_app, two_route_application


@pytest.mark.parametrize("scope", ["all", "provider", "default"])
@pytest.mark.parametrize("selection", ["draft", "session"])
async def test_endpoint_menus_separate_selected_route_from_default_and_refresh(
    tmp_path, scope, selection
):
    application = two_route_application(tmp_path, provider_id="gateway")
    configuration = ConfigurationFacade(tmp_path)
    for provider_id in ("gateway", "other"):
        configuration.add_provider(
            provider_id,
            provider_type="custom",
            protocol="openai-chat-completions",
            base_url=f"https://{provider_id}.test",
        )
        for endpoint_id in ("default", "recent"):
            configuration.save_endpoint(provider_id, endpoint_id, model=endpoint_id)
    configuration.use_provider("gateway")
    session_id = (
        application.runtime.sessions.create(current_model="gateway/recent").session.session_id
        if selection == "session"
        else None
    )
    app = OuroTuiApp(application=application, resume_session_id=session_id)

    async with run_ready_app(app) as pilot:
        if selection == "draft":
            await app._select_model("gateway/recent")
        menu = ChoiceScreen(title="ACTIONS", placeholder="Choose an action…", choices=())
        app.push_screen(menu)
        await pilot.pause()
        if scope == "provider":
            await app._choose_endpoints(configuration.provider("gateway"), menu=menu)
        else:
            await app._choose_endpoint_action("use" if scope == "default" else "list", menu=menu)
        await pilot.pause()
        choices = {choice.value: choice for choice in menu._choices}
        prefix = "" if scope == "provider" else "gateway/"
        assert choices[f"{prefix}recent"].marker == theme_for(app).selected_choice_marker
        assert choices[f"{prefix}recent"].trailing == ""
        assert choices[f"{prefix}default"].marker == ""
        assert choices[f"{prefix}default"].trailing == "default"
        if scope != "provider":
            assert choices["other/recent"].marker == ""
            assert choices["other/default"].marker == ""
            assert choices["other/default"].trailing == "default"

        if scope == "default":
            await menu._select_choice("gateway/recent")
        else:
            await app._controller.use_endpoint("gateway", "recent")
            assert await menu.refresh_choices()
        choices = {choice.value: choice for choice in menu._choices}
        assert choices[f"{prefix}recent"].marker == theme_for(app).selected_choice_marker
        assert choices[f"{prefix}recent"].trailing == "default"
        assert choices[f"{prefix}default"].marker == ""
        assert choices[f"{prefix}default"].trailing == ""

        await app._select_model("gateway/default")
        assert await menu.refresh_choices()
        choices = {choice.value: choice for choice in menu._choices}
        assert choices[f"{prefix}default"].marker == theme_for(app).selected_choice_marker
        assert choices[f"{prefix}default"].trailing == ""
        assert choices[f"{prefix}recent"].marker == ""
        assert choices[f"{prefix}recent"].trailing == "default"


@pytest.mark.parametrize("selected", [None, "deleted/endpoint"])
async def test_endpoint_menu_remains_available_without_a_resolvable_selection(
    tmp_path, monkeypatch, selected
):
    application = two_route_application(tmp_path, provider_id="gateway")
    configuration = ConfigurationFacade(tmp_path)
    configuration.add_provider(
        "gateway",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://gateway.test",
    )
    configuration.save_endpoint("gateway", "default", model="default-model")
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
        assert await app._controller.selected_model_key() == selected
        menu = ChoiceScreen(title="ACTIONS", placeholder="Choose an action…", choices=())
        app.push_screen(menu)
        await pilot.pause()
        await app._choose_endpoint_action("list", menu=menu)
        assert len(menu._choices) == 1
        assert menu._choices[0].value == "gateway/default"
        assert menu._choices[0].marker == ""
        assert menu._choices[0].trailing == "default"
