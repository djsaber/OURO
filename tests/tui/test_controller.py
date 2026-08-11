from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from ouro.application.configuration import ConfigurationFacade
from ouro.application.models import AvailableModel
from ouro.application.plugin_models import PluginCommandExecutionView, PluginCommandView
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.core.adapter import RuntimeAdapter
from ouro.core.compaction import CompactionRecord, compaction_source_digest
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
        model = await controller.configure_model(draft)
        saved = configuration.endpoint("custom", "new")

        assert model.key == "custom/new"
        assert saved.model_id == "new-model"
        assert saved.context_window == 200_000
        assert saved.max_output_tokens == 16_000
        assert saved.supports_tools is True
        assert saved.supports_vision is True
        with pytest.raises(ConfigError, match="already exists"):
            await controller.configure_model(draft)
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
        tools = {item.name: item for item in await controller.tool_configurations()}
        assert tools["Read"].effect == "read_only"
        assert tools["Write"].effect == "state_changing"

        updated = await controller.set_tool_permission("Write", "deny")
        assert updated is not None and updated.permission_override == "deny"
        assert configuration.permission_config().tools["Write"] == "deny"

        execution = await controller.update_tool_execution(
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

        saved = await controller.save_mcp_configuration(
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
        skill = next(
            item for item in (await controller.skill_catalog()).skills if item.name == "demo"
        )
        assert skill.enabled is True
        assert (await controller.validate_skill("demo")).valid is True

        await controller.set_skill_enabled("demo", enabled=False)
        disabled = next(
            item for item in (await controller.skill_catalog()).skills if item.name == "demo"
        )
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
        model = (await controller.models())[0]
        saved = await controller.save_compaction_configuration(
            TuiCompactionConfiguration(
                strategy_id="builtin.llm_summary",
                fallback_strategy_id=None,
                auto_enabled=False,
                trigger_ratio=0.7,
                target_ratio=0.2,
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
async def test_controller_forks_before_user_draft_without_mutating_source(
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
        points = await controller.branch_points()
        assert [message.role for message in points] == ["user"]
        source_before = application.runtime.store.load_session(source_session_id)

        branch = await controller.branch_session(points[0].message_id)
        source_after = application.runtime.store.load_session(source_session_id)
        loaded = await controller.load_session(branch.session.session_id)

        assert branch.session.parent_session_id == source_session_id
        assert loaded.messages == ()
        assert source_after == source_before
        assert branch.source_message_id is None
        assert branch.session.forked_from_message_id is None
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_load_session_does_not_block_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _reasoning_application(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
    )
    await controller.start()

    try:
        result = await controller.submit("open this conversation")
        entered = threading.Event()
        release = threading.Event()
        reader_threads: list[int] = []
        original_open = application.runtime.sessions.open

        def blocking_open(session_id: str, *, recover: bool = True):
            reader_threads.append(threading.get_ident())
            entered.set()
            release.wait()
            return original_open(session_id, recover=recover)

        monkeypatch.setattr(application.runtime.sessions, "open", blocking_open)
        watchdog = threading.Timer(1, release.set)
        watchdog.start()
        event_loop_thread = threading.get_ident()
        loading = asyncio.create_task(controller.load_session(result.session.session_id))

        try:
            reader_entered = await asyncio.to_thread(entered.wait, 2)
            event_loop_remained_responsive = not loading.done()
        finally:
            release.set()
            watchdog.cancel()
        loaded = await loading

        assert reader_entered is True
        assert event_loop_remained_responsive is True
        assert reader_threads != [event_loop_thread]
        assert loaded.detail.session.session_id == result.session.session_id
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_does_not_replay_events_for_completed_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _reasoning_application(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
    )
    await controller.start()

    try:
        result = await controller.submit("open completed conversation")

        def unexpected_event_page(*_args, **_kwargs):
            raise AssertionError("completed Sessions must not replay the event journal")

        monkeypatch.setattr(application.runtime.sessions, "event_page", unexpected_event_page)

        loaded = await controller.load_session(result.session.session_id)

        assert loaded.detail.session.active_run_id is None
        assert loaded.events == ()
        assert loaded.runs == (result.run,)
        assert loaded.messages
        assistant = next(message for message in loaded.messages if message.role == "assistant")
        assert assistant.model_response is not None
        assert assistant.model_response.ended_at >= assistant.model_response.started_at
        assert loaded.context_status is not None
        assert loaded.effective_context_metrics == loaded.context_status.metrics
        assert loaded.compactions == ()
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_restores_compaction_boundaries_and_effective_metrics(
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
        result = await controller.submit("conversation to compact")
        session_id = result.session.session_id
        messages = application.runtime.store.load_runtime_session(session_id).messages
        before = application.runtime.sessions.context_status(session_id)
        assert before is not None
        used = before.metrics.estimated_input_tokens
        budget = before.metrics.input_budget_tokens
        compacted_tokens = max(before.metrics.estimated_fixed_tokens, used // 2)
        compacted_metrics = before.metrics.model_copy(
            update={
                "estimated_input_tokens": compacted_tokens,
                "estimated_remaining_tokens": (
                    budget - compacted_tokens if budget is not None else None
                ),
                "estimated_utilization_ratio": (
                    compacted_tokens / budget if budget is not None else None
                ),
                "over_budget": budget is not None and compacted_tokens > budget,
            }
        )
        compaction = CompactionRecord(
            session_id=session_id,
            strategy_id="test",
            trigger="manual",
            summary="summary",
            covered_message_ids=[messages[0].message_id],
            history_boundary_message_id=messages[-1].message_id,
            source_digest=compaction_source_digest(messages[:1]),
            estimated_tokens_after=compacted_tokens,
            context_metrics_after=compacted_metrics,
        )
        application.runtime.store.append_compaction(compaction)
        application.runtime.store.activate_compaction(session_id, compaction.compaction_id)

        loaded = await controller.load_session(session_id)

        assert loaded.compactions == (compaction,)
        assert loaded.context_status == before
        assert loaded.effective_context_metrics == compacted_metrics
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_configuration_reads_do_not_block_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        entered = threading.Event()
        release = threading.Event()
        reader_threads: list[int] = []
        original_catalog = configuration.skill_catalog

        def blocking_catalog(*, include_disabled: bool = False):
            reader_threads.append(threading.get_ident())
            entered.set()
            release.wait()
            return original_catalog(include_disabled=include_disabled)

        monkeypatch.setattr(configuration, "skill_catalog", blocking_catalog)
        watchdog = threading.Timer(1, release.set)
        watchdog.start()
        event_loop_thread = threading.get_ident()
        loading = asyncio.create_task(controller.skill_catalog())

        try:
            reader_entered = await asyncio.to_thread(entered.wait, 2)
            event_loop_remained_responsive = not loading.done()
        finally:
            release.set()
            watchdog.cancel()
        await loading

        assert reader_entered is True
        assert event_loop_remained_responsive is True
        assert reader_threads != [event_loop_thread]
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
        assert await controller.session_approval_preset() == "inherit"
        await controller.set_session_approval_preset("afk")
        draft_reasoning = await controller.set_session_reasoning(
            ReasoningRequest(mode="enabled", effort="high")
        )
        assert draft_reasoning.effective.effort == "high"

        result = await controller.submit("create configured session")
        assert result.session.approval_mode == "auto"
        assert result.session.interaction_mode == "unattended"
        assert result.session.reasoning_request.effort == "high"

        await controller.set_session_approval_preset("deny")
        persisted_reasoning = await controller.set_session_reasoning(
            ReasoningRequest(mode="disabled")
        )
        record = application.runtime.store.load_session_record(result.session.session_id)
        assert record.approval_mode == "deny"
        assert record.interaction_mode == "interactive"
        assert record.reasoning_request.mode == "disabled"
        assert persisted_reasoning.effective.mode == "disabled"

        controller.new_session()
        assert await controller.session_approval_preset() == "inherit"
        assert (await controller.session_reasoning()).requested == ReasoningRequest()
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_resets_reasoning_only_when_model_route_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _reasoning_application(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
    )
    await controller.start()

    try:
        await controller.set_session_reasoning(
            ReasoningRequest(mode="enabled", effort="high")
        )
        result = await controller.submit("create configured session")
        runtime = application.runtime
        selected = AvailableModel(
            key="other/limited",
            provider_id="other",
            endpoint_id="limited",
            model_id="limited-model",
        )
        calls: list[ReasoningRequest | None] = []

        monkeypatch.setattr(runtime, "resolve_model", lambda _selector: selected)
        monkeypatch.setattr(
            runtime,
            "switch_session_model",
            lambda _session_id, _selector, *, reasoning_request=None: calls.append(
                reasoning_request
            ),
        )

        await controller.select_model(selected.key)
        monkeypatch.setattr(runtime, "current_model", lambda _session_id=None: selected)
        await controller.select_model(selected.key)

        assert result.session.reasoning_request.effort == "high"
        assert calls == [ReasoningRequest(), None]
    finally:
        await controller.close()
