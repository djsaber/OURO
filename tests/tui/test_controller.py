from __future__ import annotations

from pathlib import Path

import pytest

from ouro.application.configuration import ConfigurationFacade
from ouro.application.plugin_models import PluginCommandExecutionView, PluginCommandView
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import ConfigError
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest
from ouro.local_application import LocalApplication
from ouro.mcp import MCPServerConfigUpdate
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry, builtin_tools
from ouro.tui.controller import (
    TuiCompactionConfiguration,
    TuiController,
    TuiMCPServerConfiguration,
    TuiModelConfiguration,
    TuiToolExecutionConfiguration,
)


def _reasoning_application(tmp_path: Path) -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    tools = ToolRegistry()
    for tool in builtin_tools():
        tools.register(tool)
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
        tools=tools,
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
async def test_controller_manages_tool_permissions_and_path_scope_via_facade(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=_reasoning_application(tmp_path),
        configuration=configuration,
    )
    await controller.start()

    try:
        tools = {item.name: item for item in controller.tool_configurations()}
        assert tools["Read"].effect == "read_only"
        assert tools["Write"].effect == "state_changing"

        updated = controller.set_tool_permission("Write", "deny")
        assert updated is not None and updated.permission_override == "deny"
        assert configuration.permission_config().tools["Write"] == "deny"

        execution = controller.update_tool_execution(
            TuiToolExecutionConfiguration(
                mode="parallel",
                max_concurrency=3,
                workspace_only=False,
                max_output_chars=50_000,
            )
        )
        assert execution.mode == "parallel"
        assert execution.workspace_only is False
        assert configuration.tool_execution.snapshot().workspace_only is False
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_separates_mcp_config_from_current_runtime_lifecycle(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    configuration.save_mcp_server(
        "local",
        MCPServerConfigUpdate(command="python", auto_start=False),
    )
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=_reasoning_application(tmp_path),
        configuration=configuration,
    )
    await controller.start()

    try:
        runtime_before = controller.mcp_servers()[0]
        assert runtime_before.server_id == "local"
        assert runtime_before.status == "configured"

        saved = controller.save_mcp_configuration(
            TuiMCPServerConfiguration(
                server_id="local",
                name="Renamed",
                transport="stdio",
                target=None,
                args=["-m", "server"],
                env=None,
                inherit_env=None,
                cwd=None,
                headers=None,
                credential_refs=None,
                enabled=True,
                auto_start=False,
                auto_reconnect=True,
                max_reconnect_attempts=3,
                reconnect_backoff_seconds=0.5,
                timeout_seconds=30.0,
                health_check_interval_seconds=30.0,
                permission_mode="approval",
                tool_effect_overrides={"read": "read_only"},
            )
        )

        assert saved.name == "Renamed"
        assert saved.tool_effect_overrides == {"read": "read_only"}
        persisted = configuration.mcp.get("local")
        assert persisted is not None
        assert persisted.command == "python"
        assert persisted.args == ["-m", "server"]
        assert controller.mcp_servers()[0].name == runtime_before.name
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_manages_discovered_skill_policy_and_validation(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill.\n---\n\nUse the demo workflow.\n",
        encoding="utf-8",
    )
    configuration = ConfigurationFacade(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=_reasoning_application(tmp_path),
        configuration=configuration,
    )
    await controller.start()

    try:
        skill = next(item for item in controller.skill_catalog().skills if item.name == "demo")
        assert skill.enabled is True
        assert controller.validate_skill("demo").valid is True

        controller.set_skill_enabled("demo", enabled=False)
        disabled = next(item for item in controller.skill_catalog().skills if item.name == "demo")
        assert disabled.enabled is False
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_persists_complete_compaction_options_and_requires_session(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=_reasoning_application(tmp_path),
        configuration=configuration,
    )
    await controller.start()

    try:
        model = controller.models()[0]
        saved = controller.save_compaction_configuration(
            TuiCompactionConfiguration(
                strategy_id="builtin.llm_summary",
                fallback_strategy_id=None,
                auto_enabled=False,
                trigger_ratio=0.7,
                target_ratio=0.2,
                summary_output_tokens=2048,
                model_key=model.key,
                temperature=0.1,
                max_output_tokens=1024,
                reasoning=ReasoningRequest(mode="disabled"),
            )
        )

        assert saved.policy["auto_enabled"] is False
        assert saved.model.provider_id == model.provider_id
        assert saved.model.endpoint_id == model.endpoint_id
        assert saved.model.reasoning == ReasoningRequest(mode="disabled")
        with pytest.raises(RuntimeError, match="no active session"):
            await controller.compact()
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_resolves_plugin_commands_and_creates_required_session(
    tmp_path: Path,
) -> None:
    application = _reasoning_application(tmp_path)

    class Plugins:
        def list_commands(self) -> list[PluginCommandView]:
            return [
                PluginCommandView(
                    command_id="dev.ouro.demo:hello",
                    plugin_id="dev.ouro.demo",
                    name="hello",
                    requires_session=True,
                )
            ]

        async def execute_command(
            self,
            command_id: str,
            argument: str,
            **_kwargs: object,
        ) -> PluginCommandExecutionView:
            return PluginCommandExecutionView(
                command_id=command_id,
                output=f"hello {argument}",
            )

    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
    )
    await controller.start()
    application.plugins = Plugins()  # type: ignore[assignment]

    try:
        result = await controller.execute_plugin_command("hello", "world")

        assert result is not None
        assert result.execution.output == "hello world"
        assert controller.session_id is not None
        assert application.runtime.sessions.open(controller.session_id).session.title == "world"
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_forks_at_canonical_message_without_mutating_source(
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
        source_result = await controller.submit("fork this conversation")
        source_session_id = source_result.session.session_id
        points = controller.branch_points()
        assert [message.role for message in points] == ["user", "assistant"]

        branch = controller.branch_session(points[-1].message_id)
        source_after = application.runtime.store.load_session(source_session_id)
        loaded = controller.load_session(branch.session.session_id)

        assert branch.session.parent_session_id == source_session_id
        assert branch.session.forked_from_message_id == points[-1].message_id
        assert len(loaded.messages) == len(points)
        assert all(message.session_id == branch.session.session_id for message in loaded.messages)
        assert all(message.run_id is None for message in loaded.messages)
        assert [message.message_id for message in source_after.messages] == [
            message.message_id for message in points
        ]
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
        persisted_reasoning = controller.set_session_reasoning(ReasoningRequest(mode="disabled"))
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
