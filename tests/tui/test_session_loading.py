from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ouro.application.configuration import ConfigurationFacade
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import ConfigError
from ouro.local_application import LocalApplication
from ouro.providers import FauxProvider
from ouro.tui.app import OuroTuiApp
from ouro.tui.controller import TuiController
from ouro.tui.screens import ChoiceScreen
from ouro.tui.widgets import Composer, ConversationView, StatusLine
from tests.tui.helpers import run_ready_app, two_route_application


@pytest.mark.parametrize("with_history", [False, True])
async def test_unavailable_session_model_can_be_replaced(
    tmp_path: Path, with_history: bool
) -> None:
    application = two_route_application(tmp_path)
    controller = TuiController(lambda _event: None, lambda _status: None, application=application)
    await controller.start()
    try:
        await controller.select_model("faux/recent")
        if with_history:
            session = (await controller.submit("Saved conversation")).session
        else:
            session = application.runtime.create_session(
                title="Empty conversation", initial_model="faux/recent"
            )
        del application.runtime.models._models["faux/recent"]
        await controller.load_session(session.session_id)

        with pytest.raises(ConfigError, match="model not found"):
            await controller.select_model("missing/route")
        assert application.runtime.sessions.open(session.session_id).session.current_model == (
            "faux/recent"
        )

        selected = await controller.select_model("faux/default")
        result = await controller.submit("Continue conversation")
        assert selected.key == "faux/default"
        assert result.session.session_id == session.session_id
        assert result.run.status == "completed"
        assert result.session.current_model == "faux/default"
    finally:
        await controller.close()
        await application.close()


@pytest.mark.parametrize("entry", ["resume", "picker"])
async def test_tui_restores_history_and_observation_without_the_old_model(
    tmp_path: Path, entry: str
) -> None:
    application = two_route_application(tmp_path)
    async with application:
        result = await application.runtime.run("Saved conversation", initial_model="faux/recent")
        session_id = result.session.session_id
        durable = application.runtime.sessions.effective_context_metrics(session_id)
        del application.runtime.models._models["faux/recent"]
        app = OuroTuiApp(
            application=application,
            resume_session_id=session_id if entry == "resume" else None,
        )
        async with run_ready_app(app, size=(100, 32)) as pilot:
            if entry == "picker":
                await pilot.press("ctrl+s")
                async with asyncio.timeout(5):
                    while not isinstance(app.screen, ChoiceScreen):
                        await pilot.pause()
                await pilot.press("enter")
                async with asyncio.timeout(5):
                    while app._projection is None:
                        await pilot.pause()
            assert not isinstance(app.screen, ChoiceScreen)
            assert app._controller.session_id == session_id
            assert app._projection is not None
            assert app.query_one(StatusLine)._context_metrics == durable
            assert app.query_one(Composer).disabled is False
            assert "Saved" in app.export_screenshot()
            assert app.query_one(ConversationView).showing_welcome is False
            assert app._reasoning_state is None

            observation = await app._controller.observe_session(session_id)
            try:
                assert observation.loaded.messages
                assert observation.loaded.effective_context_metrics == durable
                assert observation.loaded.detail.session.current_model == "faux/recent"
            finally:
                observation.subscription.close()

            await app._select_model("faux/default")
            assert app._active_model == ""
            assert app._reasoning_state is not None
            assert application.runtime.sessions.open(session_id).session.current_model == (
                "faux/default"
            )


@pytest.mark.parametrize("backend", ["files", "sqlite"])
@pytest.mark.parametrize("change", ["endpoint_removed", "provider_removed", "provider_disabled"])
async def test_configured_route_changes_do_not_block_session_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, change: str
) -> None:
    from ouro.providers.factory import _ADAPTER_BUILDERS

    monkeypatch.setitem(
        _ADAPTER_BUILDERS,
        "openai-chat-completions",
        lambda context: RuntimeAdapter(FauxProvider(), context.model),
    )
    configuration = ConfigurationFacade(tmp_path)
    original = configuration.add_provider(
        display_name="Local fixture",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://fixture.invalid/v1",
    )
    assert original.changed_provider_id is not None
    configuration.add_endpoint(
        original.changed_provider_id, display_name="Local", model="local-model"
    )
    configuration.use_provider(original.changed_provider_id)
    application = LocalApplication.build(tmp_path, session_backend=backend)
    controller = TuiController(lambda _event: None, lambda _status: None, application=application)
    await controller.start()
    try:
        result = await controller.submit("Saved conversation")
        runtime = application.runtime
        original_route = result.session.current_model
        created = configuration.add_provider(
            display_name="Remote",
            provider_type="custom",
            protocol="openai-chat-completions",
            base_url="https://example.invalid/v1",
            api_key="test-key",
        )
        provider_id = created.changed_provider_id
        assert provider_id is not None
        endpoints = configuration.add_endpoint(provider_id, display_name="Chat", model="test-model")
        endpoint_id = endpoints.changed_endpoint_id
        assert endpoint_id is not None
        route = f"{provider_id}/{endpoint_id}"
        await controller.select_model(route)
        if change == "endpoint_removed":
            configuration.remove_endpoint(provider_id, endpoint_id)
        elif change == "provider_removed":
            configuration.remove_provider(provider_id)
        else:
            configuration.update_provider(provider_id, enabled=False)

        loaded = await controller.load_session(result.session.session_id)
        assert loaded.messages
        assert loaded.detail.session.current_model == route
        assert loaded.effective_context_metrics == runtime.sessions.effective_context_metrics(
            result.session.session_id
        )
        with pytest.raises(ConfigError):
            await controller.select_model(route)
        assert original_route is not None
        await controller.select_model(original_route)
        continued = await controller.submit("Continue after configuration change")
        assert continued.run.status == "completed"
        assert continued.session.session_id == result.session.session_id
    finally:
        await controller.close()
        await application.close()
