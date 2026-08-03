from __future__ import annotations

from pathlib import Path

import pytest

from ouro.application.configuration import ConfigurationFacade
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import ConfigError
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest
from ouro.local_application import LocalApplication
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry
from ouro.tui.controller import TuiController, TuiModelConfiguration


def _reasoning_application(tmp_path: Path) -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent(type="text_delta", text="done"),
                        ProviderStreamEvent(type="done"),
                    ]
                ]
            ),
            ModelMetadata(
                model_id="thinking-model",
                provider_id="faux",
                reasoning=ReasoningCapability(
                    activation="toggle",
                    supported_efforts=("low", "high"),
                    default=ReasoningRequest(mode="enabled", effort="low"),
                    output="summary",
                ),
            ),
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


@pytest.mark.asyncio
async def test_controller_configures_new_model_through_application_facade(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    configuration.add_provider(
        "custom",
        provider_type="openai-compatible",
        base_url="https://example.invalid/v1",
        api_key="test-key",
    )
    configuration.save_endpoint(
        "custom",
        "base",
        model="base-model",
        context_window=100_000,
        max_output_tokens=8_000,
        make_default=True,
    )
    configuration.use_provider("custom")
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=LocalApplication.build(tmp_path),
        configuration=configuration,
    )
    await controller.start()
    draft = TuiModelConfiguration(
        provider_id="custom",
        endpoint_id="new",
        model_id="new-model",
        context_window=200_000,
        max_output_tokens=16_000,
        supports_tools=True,
        supports_vision=True,
    )

    try:
        model = controller.configure_model(draft)
        saved = configuration.endpoint("custom", "new")

        assert model.key == "custom/new"
        assert saved.model_id == "new-model"
        assert saved.context_window == 200_000
        assert saved.max_output_tokens == 16_000
        assert saved.supports_tools is True
        assert saved.supports_vision is True
        with pytest.raises(ConfigError, match="already exists"):
            controller.configure_model(draft)
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_applies_draft_settings_and_persists_existing_session(
    tmp_path: Path,
) -> None:
    application = _reasoning_application(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
    )
    await controller.start()

    try:
        assert controller.session_approval_preset() == "inherit"
        controller.set_session_approval_preset("afk")
        draft_reasoning = controller.set_session_reasoning(
            ReasoningRequest(mode="enabled", effort="high")
        )
        assert draft_reasoning.effective.effort == "high"

        result = await controller.submit("create configured session")
        assert result.session.approval_mode == "auto"
        assert result.session.interaction_mode == "unattended"
        assert result.session.reasoning_request.effort == "high"

        controller.set_session_approval_preset("deny")
        persisted_reasoning = controller.set_session_reasoning(
            ReasoningRequest(mode="disabled")
        )
        record = application.runtime.store.load_session_record(result.session.session_id)
        assert record.approval_mode == "deny"
        assert record.interaction_mode == "interactive"
        assert record.reasoning_request.mode == "disabled"
        assert persisted_reasoning.effective.mode == "disabled"

        controller.new_session()
        assert controller.session_approval_preset() == "inherit"
        assert controller.session_reasoning().requested == ReasoningRequest()
    finally:
        await controller.close()
