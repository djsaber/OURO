from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from ouro.application.configuration import ConfigurationFacade
from ouro.application.plugin_models import PluginCommandExecutionView, PluginCommandView
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.application.usage_models import UsageSummary, UsageSummaryGroup, UsageTotals
from ouro.config import reasoning_preset
from ouro.core.adapter import RuntimeAdapter
from ouro.core.agents import ToolSelection
from ouro.core.billing import UsageFilter
from ouro.core.compaction import CompactionRecord, compaction_source_digest
from ouro.core.errors import ConfigError
from ouro.core.events import EventRecord
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.reasoning import ReasoningCapability, ReasoningRequest
from ouro.core.session_controls import AttributedSessionInput, AttributedSessionInputBatch
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolInvocation
from ouro.local_application import LocalApplication
from ouro.mcp import MCPServerConfigUpdate
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.skills import discover_skill_catalog
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult, builtin_tools
from ouro.tui.controller import (
    TuiAgentDefinitionConfiguration,
    TuiAgentRuntimeConfiguration,
    TuiCompactionConfiguration,
    TuiController,
    TuiEndpointConfiguration,
    TuiMCPServerConfiguration,
    TuiPluginInstallConfiguration,
    TuiProviderConfiguration,
    TuiSystemStatus,
    TuiToolExecutionConfiguration,
    project_usage_system_status,
)
from tests.tui.helpers import StaticRouteCatalog, two_route_application


def test_usage_status_projects_cache_totals_and_complete_recent_daily_detail() -> None:
    groups = tuple(
        UsageSummaryGroup(
            key=f"2026-08-{day:02d}",
            totals=UsageTotals(
                request_count=day,
                input_tokens=day * 100,
                output_tokens=day * 10,
                cache_read_tokens=day * 25,
                estimated_cost=day / 100,
                priced_request_count=day,
            ),
        )
        for day in range(1, 12)
    )
    summary = UsageSummary(
        usage_filter=UsageFilter(),
        totals=UsageTotals(
            request_count=66,
            input_tokens=6_600,
            output_tokens=660,
            cache_read_tokens=1_650,
        ),
        group_by="date",
        groups=groups,
    )

    status = project_usage_system_status(TuiSystemStatus(), summary)

    assert status.usage_cache_read_tokens == 1_650
    assert [day.date for day in status.usage_daily] == [
        f"2026-08-{day:02d}" for day in range(2, 12)
    ]
    assert status.usage_daily[-1].request_count == 11
    assert status.usage_daily[-1].input_tokens == 1_100
    assert status.usage_daily[-1].output_tokens == 110
    assert status.usage_daily[-1].cache_read_tokens == 275
    assert status.usage_daily[-1].total_tokens == 1_210


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
                    default_effort="low",
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
async def test_controller_detaches_event_sink_when_shared_application_stays_open(
    tmp_path: Path,
) -> None:
    application = _reasoning_application(tmp_path)
    published: list[EventRecord] = []
    controller = TuiController(
        published.append,
        lambda _status: None,
        application=application,
        close_application=False,
    )
    await controller.start()
    session = application.runtime.sessions.create(title="Shared runtime").session
    application.runtime.store.save_run(
        RunRecord(run_id="run_shared", session_id=session.session_id)
    )
    await controller.load_session(session.session_id)

    try:
        await application.runtime.event_recorder.publish(
            session_id=session.session_id,
            run_id="run_shared",
            event_type="run_started",
            payload={"trigger": "user"},
        )
        await application.runtime.drain_events()
        assert [event.event_type for event in published] == ["run_started"]

        await controller.close()
        await application.runtime.event_recorder.publish(
            session_id=session.session_id,
            run_id="run_shared",
            event_type="run_completed",
            payload={},
        )
        await application.runtime.drain_events()

        assert [event.event_type for event in published] == ["run_started"]
    finally:
        await controller.close()
        await application.close()


@pytest.mark.asyncio
async def test_controller_new_session_reuses_previous_session_workspace(tmp_path: Path) -> None:
    application = two_route_application(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
    )
    selected = tmp_path / "selected"
    selected.mkdir()
    await controller.start()

    try:
        assert controller.draft_workspace == str(tmp_path.resolve())

        workspace = await controller.set_workspace(str(selected))
        status = await controller.system_status()
        result = await controller.submit("use selected workspace")

        assert workspace == str(selected.resolve())
        assert status.workspace == str(selected.resolve())
        assert status.model_names == ("default-model", "recent-model")
        assert result.session.workspace == str(selected.resolve())
        with pytest.raises(ValueError, match="before creating a session"):
            await controller.set_workspace(str(tmp_path))

        await controller.new_session()
        assert controller.draft_workspace == str(selected.resolve())

        inherited = await controller.submit("reuse selected workspace")
        assert inherited.session.workspace == str(selected.resolve())
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_rejects_submission_without_configured_model(tmp_path: Path) -> None:
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=LocalApplication.build(tmp_path),
    )
    await controller.start()

    try:
        with pytest.raises(ConfigError, match="add a provider and endpoint"):
            await controller.submit("draft")
        assert controller.session_id is None
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_rejects_missing_or_non_directory_workspace(tmp_path: Path) -> None:
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=two_route_application(tmp_path),
    )
    file_path = tmp_path / "file.txt"
    file_path.write_text("not a directory", encoding="utf-8")
    await controller.start()

    try:
        with pytest.raises(ValueError, match="does not exist"):
            await controller.set_workspace(str(tmp_path / "missing"))
        with pytest.raises(ValueError, match="not a directory"):
            await controller.set_workspace(str(file_path))
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_new_session_reuses_previous_session_model(
    tmp_path: Path,
) -> None:
    application = two_route_application(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
    )
    await controller.start()

    try:
        first = await controller.submit("first session")
        assert first.session.current_model == "faux/default"
        assert first.run.model == "default-model"

        await controller.select_model("faux/recent")
        draft = await controller.new_session()

        assert draft.key == "faux/recent"

        second = await controller.submit("second session")
        assert second.session.current_model == "faux/recent"
        assert second.run.model == "recent-model"
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_new_session_keeps_welcome_model_selection(
    tmp_path: Path,
) -> None:
    application = two_route_application(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
    )
    await controller.start()

    try:
        await controller.new_session()
        await controller.select_model("faux/recent")

        draft = await controller.new_session()
        assert draft.key == "faux/recent"

        result = await controller.submit("drafted session")
        assert result.session.current_model == "faux/recent"
        assert result.run.model == "recent-model"
    finally:
        await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("damaged_payload", [None, "not valid session json"])
async def test_controller_new_session_ignores_unreadable_previous_session(
    tmp_path: Path,
    damaged_payload: str | None,
) -> None:
    application = two_route_application(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
    )
    await controller.start()

    try:
        result = await controller.submit("session that becomes unreadable")
        session_id = result.session.session_id
        session_path = tmp_path / "sessions" / session_id / "session.json"
        if damaged_payload is None:
            session_path.unlink()
        else:
            session_path.write_text(damaged_payload, encoding="utf-8")

        draft = await controller.new_session()

        assert controller.session_id is None
        assert draft.key == "faux/default"
        assert controller.draft_workspace == str(tmp_path.resolve())
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_deleting_current_session_invalidates_current_pointer(
    tmp_path: Path,
) -> None:
    application = two_route_application(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
    )
    await controller.start()

    try:
        result = await controller.submit("session to delete")
        session_id = result.session.session_id
        await controller.set_session_archived(session_id, archived=True)

        removed = await controller.delete_sessions((session_id,))
        draft = await controller.new_session()

        assert removed == (session_id,)
        assert controller.session_id is None
        assert draft.key == "faux/default"
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_deletes_unreadable_session_without_archiving(
    tmp_path: Path,
) -> None:
    application = two_route_application(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
    )
    await controller.start()

    try:
        session = application.runtime.sessions.create(
            session_id="ses_unreadable",
            title="Unreadable",
        ).session
        session_path = tmp_path / "sessions" / session.session_id / "session.json"
        session_path.write_text("{broken", encoding="utf-8")
        application.runtime.store.rebuild_catalog()

        entries = {entry.session_id: entry for entry in await controller.sessions()}
        removed = await controller.delete_sessions((session.session_id,))

        assert entries[session.session_id].health == "corrupt"
        assert entries[session.session_id].error_detail is not None
        assert removed == (session.session_id,)
        assert not session_path.parent.exists()
        assert await controller.sessions() == ()
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_deletes_catalog_error_without_loading_canonical_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = two_route_application(tmp_path)
    session = application.runtime.sessions.create(
        session_id="ses_invalid_catalog_delete",
        title="Readable canonical session",
    ).session
    index_path = application.runtime.store._catalog.index_path
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    indexed = next(item for item in payload["sessions"] if item["session_id"] == session.session_id)
    indexed.pop("session_kind")
    index_path.write_text(json.dumps(payload), encoding="utf-8")
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
    )
    await controller.start()

    def unexpected_canonical_read(*_args, **_kwargs):
        raise AssertionError("TUI deletion must not decode canonical Session records")

    load_session_record = application.runtime.store.load_session_record
    monkeypatch.setattr(
        application.runtime.store,
        "load_session_record",
        unexpected_canonical_read,
    )
    monkeypatch.setattr(
        application.runtime.store,
        "list_sessions",
        unexpected_canonical_read,
    )

    try:
        entries = {entry.session_id: entry for entry in await controller.sessions()}
        removed = await controller.delete_sessions((session.session_id,))

        assert entries[session.session_id].catalog_error is not None
        assert removed == (session.session_id,)
        with pytest.raises(FileNotFoundError):
            load_session_record(session.session_id)
        assert await controller.sessions() == ()
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_start_defers_session_catalog_until_requested(
    tmp_path: Path,
    monkeypatch,
) -> None:
    application = _reasoning_application(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
        configuration=ConfigurationFacade(tmp_path),
    )

    def unexpected_catalog(*_args, **_kwargs):
        raise AssertionError("TUI startup must not load the Session catalog")

    monkeypatch.setattr(application.runtime.sessions, "catalog", unexpected_catalog)

    bootstrap = await controller.start()

    try:
        assert bootstrap.sessions == ()
    finally:
        await controller.close()


class _SkillPromptCapturingProvider(FauxProvider):
    def __init__(self) -> None:
        super().__init__()
        self.system_prompts: list[str] = []

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        self.system_prompts.append(system_prompt)
        async for event in super().stream(
            model=model,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
        ):
            yield event


def _skill_prompt_application(
    tmp_path: Path,
    provider: _SkillPromptCapturingProvider,
) -> LocalApplication:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="skill-prompt", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        skills=tuple(discover_skill_catalog(home=tmp_path).skills),
        default_workspace=tmp_path,
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    return LocalApplication.from_runtime(runtime)


@pytest.mark.asyncio
async def test_controller_configures_provider_endpoint_through_application_facade(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    created = configuration.add_provider(
        display_name="Custom",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://example.invalid/v1",
        api_key="test-key",
    )
    provider_id = created.changed_provider_id
    assert provider_id is not None
    base_catalog = configuration.add_endpoint(
        provider_id,
        display_name="Base",
        model="base-model",
        context_window=100_000,
        max_output_tokens=8_000,
        make_default=True,
    )
    base_id = base_catalog.changed_endpoint_id
    assert base_id is not None
    configuration.use_provider(provider_id)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=LocalApplication.build(tmp_path),
        configuration=configuration,
    )
    await controller.start()
    draft = TuiEndpointConfiguration(
        display_name="New",
        model_id="new-model",
        context_window=200_000,
        max_output_tokens=16_000,
        supports_tools=True,
        supports_vision=True,
        reasoning=reasoning_preset("thinking-toggle"),
        request_options={"seed": 7},
    )

    try:
        endpoint = await controller.add_endpoint(provider_id, draft)
        saved = configuration.endpoint(provider_id, endpoint.endpoint_id)

        assert endpoint.endpoint_id.startswith("endpoint_")
        assert saved.model_id == "new-model"
        assert saved.context_window == 200_000
        assert saved.max_output_tokens == 16_000
        assert saved.supports_tools is True
        assert saved.supports_vision is True
        assert saved.reasoning == reasoning_preset("thinking-toggle")
        assert saved.request_options == {"seed": 7}
        assert f"{provider_id}/{endpoint.endpoint_id}" in {
            model.key for model in await controller.models()
        }
        duplicate = await controller.add_endpoint(provider_id, draft)
        assert duplicate.endpoint_id != endpoint.endpoint_id

        changed = await controller.update_endpoint(
            provider_id,
            endpoint.endpoint_id,
            TuiEndpointConfiguration(
                **{
                    **draft.__dict__,
                    "model_id": "new-model-v2",
                    "supports_structured_output": True,
                    "reasoning": reasoning_preset("none"),
                    "request_options": {"seed": 9},
                }
            ),
        )
        assert changed.model_id == "new-model-v2"
        assert changed.supports_structured_output is True
        assert changed.reasoning == reasoning_preset("none")
        assert changed.request_options == {"seed": 9}
        assert (await controller.use_endpoint(provider_id, endpoint.endpoint_id)).default is True
        await controller.remove_endpoint(provider_id, endpoint.endpoint_id)
        await controller.remove_endpoint(provider_id, duplicate.endpoint_id)
        endpoints = (await controller.provider_endpoints(provider_id)).endpoints
        assert {item.endpoint_id for item in endpoints} == {base_id}
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_first_tui_endpoint_becomes_default_when_no_route_is_configured(
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
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=LocalApplication.build(tmp_path),
        configuration=configuration,
    )
    await controller.start()
    try:
        assert (await controller.provider_catalog()).default_provider_id is None
        first = await controller.add_endpoint(
            provider_id,
            TuiEndpointConfiguration(
                display_name="First",
                model_id="first-model",
                context_window=128_000,
                max_output_tokens=8192,
            ),
        )
        catalog = await controller.provider_catalog()
        assert catalog.default_provider_id == provider_id
        assert catalog.providers[0].default_endpoint == first.endpoint_id

        await controller.add_endpoint(
            provider_id,
            TuiEndpointConfiguration(
                display_name="Second",
                model_id="second-model",
                context_window=128_000,
                max_output_tokens=8192,
            ),
        )
        catalog = await controller.provider_catalog()
        assert catalog.providers[0].default_endpoint == first.endpoint_id
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_configures_preset_and_custom_providers(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=LocalApplication.build(tmp_path),
        configuration=configuration,
    )
    await controller.start()

    try:
        templates = await controller.provider_templates()
        assert {item.template_id for item in templates} >= {"openai", "anthropic", "kimi"}

        preset = await controller.add_provider(
            TuiProviderConfiguration(
                display_name="Primary",
                provider_type="openai",
                base_url=None,
                api_path=None,
                api_key=None,
                api_key_env="PRIVATE_OPENAI_KEY",
                timeout=45.0,
                provider_options={"service_tier": "flex"},
                make_default=True,
            )
        )
        assert preset.default is True
        assert preset.endpoint_count == 7
        assert preset.credential.reference == "env:PRIVATE_OPENAI_KEY"
        assert preset.provider_options == {"service_tier": "flex"}
        primary_id = preset.provider_id

        target = next(item for item in preset.endpoints if item.model_id == "gpt-5.6-sol")
        terra = next(item for item in preset.endpoints if item.model_id == "gpt-5.6-terra")
        untouched_reasoning = configuration.endpoint(
            primary_id,
            terra.endpoint_id,
        ).reasoning
        overridden = await controller.update_endpoint(
            primary_id,
            target.endpoint_id,
            TuiEndpointConfiguration(
                display_name=target.display_name,
                model_id=target.model_id,
                context_window=target.context_window,
                max_output_tokens=target.max_output_tokens,
                supports_tools=target.supports_tools,
                supports_vision=target.supports_vision,
                supports_streaming=target.supports_streaming,
                supports_structured_output=target.supports_structured_output,
                reasoning=reasoning_preset("none"),
            ),
        )
        assert overridden.reasoning == reasoning_preset("none")
        assert (
            configuration.endpoint(
                primary_id,
                terra.endpoint_id,
            ).reasoning
            == untouched_reasoning
        )

        custom = await controller.add_provider(
            TuiProviderConfiguration(
                display_name="Gateway",
                provider_type="custom",
                protocol="anthropic-messages",
                base_url="https://gateway.example.invalid",
                api_path="/v1/messages",
                api_key=None,
                api_key_env="GATEWAY_KEY",
                timeout=30.0,
                provider_options={"tool_choice": "none", "top_p": 0.8},
            )
        )
        assert custom.endpoint_count == 0
        assert custom.protocol == "anthropic-messages"
        assert custom.provider_options == {"tool_choice": "none", "top_p": 0.8}
        gateway_id = custom.provider_id

        disabled = await controller.set_provider_enabled(gateway_id, enabled=False)
        assert disabled.enabled is False
        await controller.remove_provider(gateway_id)
        assert {item.provider_id for item in (await controller.provider_catalog()).providers} == {
            primary_id
        }
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
        application=(application := _reasoning_application(tmp_path)),
        configuration=configuration,
    )
    await controller.start()

    try:
        tools = {item.name: item for item in await controller.tool_configurations()}
        assert tools["Read"].effect == "read_only"
        assert tools["Write"].effect == "state_changing"
        active_executor = application.runtime._tool_calls_for_run("active").executor

        updated = await controller.set_tool_permission("Write", "deny")
        assert updated is not None and updated.permission_override == "deny"
        assert configuration.permission_config().tools["Write"] == "deny"
        denied = active_executor.preflight(
            ToolInvocation(
                session_id="session",
                run_id="active",
                tool_name="Write",
                arguments={"path": "unused.txt", "content": "content"},
            )
        )
        assert denied.invocation.permission_mode == "deny"

        automatic = await controller.set_tool_permission("Write", "auto")
        assert automatic is not None and automatic.permission_override == "auto"
        ready = active_executor.preflight(
            ToolInvocation(
                session_id="session",
                run_id="active",
                tool_name="Write",
                arguments={"path": "unused.txt", "content": "content"},
            )
        )
        assert ready.status == "ready"

        inherited = await controller.set_tool_permission("Write", None)
        assert inherited is not None and inherited.permission_override is None
        protected = active_executor.preflight(
            ToolInvocation(
                session_id="session",
                run_id="active",
                tool_name="Write",
                arguments={"path": "unused.txt", "content": "content"},
            )
        )
        assert protected.status == "approval_required"

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
async def test_controller_manages_subagent_policy_and_definitions(
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
        runtime = await controller.agent_runtime_configuration()
        updated_runtime = await controller.update_agent_runtime(
            TuiAgentRuntimeConfiguration(
                expected_revision=runtime.revision,
                allowed_agent_id_patterns=("review-*",),
                allowed_model_patterns=("faux",),
                allowed_execution_modes=("sync",),
                allow_write=False,
                allow_network=False,
                allow_mcp=False,
                allow_external_paths=False,
            )
        )
        assert updated_runtime.spawn_policy.allowed_agent_id_patterns == ("review-*",)
        assert updated_runtime.spawn_policy.allowed_execution_modes == ("sync",)
        assert updated_runtime.spawn_policy.allow_write is False

        catalog = await controller.agent_definition_catalog()
        created = await controller.create_agent_definition(
            TuiAgentDefinitionConfiguration(
                agent_id="review-code",
                name="Code reviewer",
                description="Review a bounded code change.",
                system_prompt="Review the change and report concrete findings.",
                tool_selection=ToolSelection(mode="all"),
                model="faux",
            ),
            expected_revision=catalog.revision,
        )
        assert any(item.agent_id == "review-code" for item in created.definitions)

        detail = await controller.agent_definition("review-code")
        assert detail.system_prompt.startswith("Review the change")
        assert detail.model == "faux"
        updated = await controller.update_agent_definition(
            TuiAgentDefinitionConfiguration(
                agent_id=detail.agent_id,
                name=detail.name,
                description="Review code and tests.",
                system_prompt=detail.system_prompt,
                tool_selection=detail.tool_selection,
                model=None,
            ),
            expected_revision=created.revision,
        )
        assert (
            next(item for item in updated.definitions if item.agent_id == "review-code").description
            == "Review code and tests."
        )
        assert (
            next(item for item in updated.definitions if item.agent_id == "review-code").model
            is None
        )

        deleted = await controller.delete_agent_definition(
            "review-code",
            expected_revision=updated.revision,
        )
        assert all(item.agent_id != "review-code" for item in deleted.definitions)
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_rejects_overrides_for_contract_controlled_plugin_tools(
    tmp_path: Path,
) -> None:
    configuration = ConfigurationFacade(tmp_path)
    application = _reasoning_application(tmp_path)
    application.runtime.tools.register(
        Tool(
            definition=ToolDefinition(
                name="controlled__run",
                description="Run with contract-controlled permission.",
                permission_mode="auto",
                permission_control="tool",
                source="third_party_plugin",
                metadata={"plugin_id": "dev.ouro.controlled"},
            ),
            handler=lambda invocation: ToolResult(
                tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name,
            ),
        )
    )
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
        configuration=configuration,
    )
    await controller.start()

    try:
        with pytest.raises(ConfigError, match="controlled by its contract"):
            await controller.set_tool_permission("controlled__run", None)
        assert configuration.permission_config().plugins == {}
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_tool_catalog_includes_search_and_todolist_as_host_builtins(
    tmp_path: Path,
) -> None:
    application = _reasoning_application(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
        configuration=ConfigurationFacade(tmp_path),
    )
    await controller.start()

    try:
        tools = {item.name: item for item in await controller.tool_configurations()}
        for name in ("Glob", "Grep", "TodoList"):
            assert tools[name].source == "builtin"
            assert tools[name].plugin_id is None
            assert tools[name].plugin_name is None
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_manages_plugins_through_local_application_boundary(
    tmp_path: Path,
) -> None:
    application = _reasoning_application(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
        configuration=ConfigurationFacade(tmp_path),
    )
    await controller.start()

    try:
        plugins = {item.plugin_id: item for item in controller.plugins()}
        assert plugins["dev.ouro.agent_context"].plugin_class == "builtin"
        assert plugins["dev.ouro.agent_context"].status == "active"

        disabled = await controller.disable_plugin("dev.ouro.agent_context")
        assert disabled.status == "disabled"
        assert controller.plugin("dev.ouro.agent_context").desired_enabled is False

        enabled = await controller.enable_plugin("dev.ouro.agent_context")
        assert enabled.status == "active"
        detail = controller.plugin("dev.ouro.agent_context")
        assert detail.desired_enabled is True
        assert detail.configuration_supported is False

        checked = await controller.check_plugin("dev.ouro.agent_context")
        assert checked.status == "active"

        with pytest.raises(Exception, match="does not exist|not found|No such file"):
            await controller.install_plugin(
                TuiPluginInstallConfiguration(source=str(tmp_path / "missing-plugin"))
            )
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_installs_and_configures_user_plugin(tmp_path: Path) -> None:
    source = tmp_path / "user-plugin"
    manifest_dir = source / ".ouro-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "plugin_id": "dev.ouro.tui_config",
                "name": "TUI configuration",
                "version": "1.0.0",
                "namespace": "tui_config",
                "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^3.0.0"},
                "runtime": {"kind": "declarative"},
                "contributes": {},
                "permissions": [],
                "configuration": {"schema": "./configuration.schema.json", "version": 1},
                "dependencies": {"plugins": []},
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    (source / "configuration.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"mode": {"type": "string"}},
                "required": ["mode"],
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=_reasoning_application(tmp_path),
        configuration=ConfigurationFacade(tmp_path),
    )
    await controller.start()

    try:
        installed = await controller.install_plugin(
            TuiPluginInstallConfiguration(source=str(source))
        )
        assert installed.plugin_id == "dev.ouro.tui_config"
        installed_detail = controller.plugin(installed.plugin_id)
        assert installed_detail.configuration_supported is True

        configured = await controller.configure_plugin(
            installed.plugin_id,
            {"mode": "safe"},
            {},
        )
        assert configured.status == "disabled"
        detail = controller.plugin(installed.plugin_id)
        assert detail.configuration_values == {"mode": "safe"}
        assert detail.credential_refs == {}
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_creates_mcp_configuration(tmp_path: Path) -> None:
    configuration = ConfigurationFacade(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=_reasoning_application(tmp_path),
        configuration=configuration,
    )
    await controller.start()

    try:
        saved = await controller.save_mcp_configuration(
            TuiMCPServerConfiguration(
                server_id="local",
                name="Local server",
                transport="stdio",
                target="python",
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
                tool_effect_overrides=None,
            )
        )

        assert saved.server_id == "local"
        assert configuration.mcp_server("local") == saved
        assert controller.mcp_servers()[0].server_id == "local"
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_hot_updates_mcp_tool_policy_without_reconfiguring_connection(
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
                permission_mode="deny",
                tool_effect_overrides={"read": "read_only"},
            )
        )

        assert saved.name == "Renamed"
        assert saved.permission_mode == "deny"
        assert saved.tool_effect_overrides == {"read": "read_only"}
        persisted = configuration.mcp.get("local")
        assert persisted is not None
        assert persisted.command == "python"
        assert persisted.args == ["-m", "server"]
        runtime_config = controller._application.mcp.manager.get("local").config
        assert runtime_config.permission_mode == "deny"
        assert runtime_config.tool_effect_overrides == {"read": "read_only"}
        assert controller.mcp_servers()[0].name == "Renamed"

        disabled = await controller.set_mcp_enabled("local", enabled=False)
        assert disabled.enabled is False
        assert configuration.mcp_server("local").enabled is False
        assert controller.mcp_servers()[0].status == "disabled"

        enabled = await controller.set_mcp_enabled("local", enabled=True)
        assert enabled.enabled is True
        assert configuration.mcp_server("local").enabled is True
        assert controller.mcp_servers()[0].status == "configured"
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
        assert controller.runtime_skill_names() == ()
        assert controller.skill_invocation_options() == ()

        await controller.set_skill_enabled("demo", enabled=True)
        assert controller.runtime_skill_names() == ("demo",)
        assert [item.name for item in controller.skill_invocation_options()] == ["demo"]
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_updates_skill_directory_sources_and_live_runtime(tmp_path: Path) -> None:
    ouro_skill = tmp_path / "skills" / "ouro-skill"
    codex_skill = tmp_path / ".codex" / "skills" / "codex-skill"
    ouro_skill.mkdir(parents=True)
    codex_skill.mkdir(parents=True)
    (ouro_skill / "SKILL.md").write_text(
        "---\nname: ouro-skill\ndescription: OURO skill.\n---\n",
        encoding="utf-8",
    )
    (codex_skill / "SKILL.md").write_text(
        "---\nname: codex-skill\ndescription: Codex skill.\n---\n",
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
        default_policy = await controller.skill_policy()
        assert default_policy.enabled is True
        assert all(source.enabled for source in default_policy.sources)

        disabled_codex = await controller.update_skill_discovery(
            source_updates={"codex.user": False}
        )
        assert disabled_codex.disabled_sources == ["codex.user"]
        assert controller.runtime_skill_names() == ("ouro-skill",)

        discovery_off = await controller.update_skill_discovery(enabled=False)
        assert discovery_off.enabled is False
        assert controller.runtime_skill_names() == ()

        discovery_on = await controller.update_skill_discovery(enabled=True)
        assert discovery_on.enabled is True
        assert discovery_on.disabled_sources == ["codex.user"]
        assert controller.runtime_skill_names() == ("ouro-skill",)

        await controller.update_skill_discovery(source_updates={"codex.user": True})
        assert controller.runtime_skill_names() == ("codex-skill", "ouro-skill")
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_rolls_back_skill_source_when_runtime_refresh_fails(
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

    async def fail_refresh(_configuration: ConfigurationFacade) -> None:
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(controller, "_refresh_runtime_skills", fail_refresh)
    try:
        with pytest.raises(RuntimeError, match="refresh failed"):
            await controller.update_skill_discovery(source_updates={"ouro.user": False})
        assert configuration.skill_policy().disabled_sources == []
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_skill_catalog_tracks_live_plugin_skills(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"skills": {"enabled": False}}),
        encoding="utf-8",
    )
    source = tmp_path / "skill-plugin"
    manifest_dir = source / ".ouro-plugin"
    skill_dir = source / "skills" / "calendar"
    manifest_dir.mkdir(parents=True)
    skill_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "plugin_id": "dev.ouro.tui_skill",
                "name": "TUI Skill",
                "version": "1.0.0",
                "namespace": "tui_skill",
                "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^3.0.0"},
                "runtime": {"kind": "declarative"},
                "contributes": {"skills": [{"id": "calendar", "path": "./skills/calendar"}]},
                "permissions": [],
                "dependencies": {"plugins": []},
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        "---\nname: calendar\ndescription: Manage calendars.\n---\n\nUse calendars.\n",
        encoding="utf-8",
    )
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=_reasoning_application(tmp_path),
        configuration=ConfigurationFacade(tmp_path),
    )
    await controller.start()

    try:
        installed = await controller.install_plugin(
            TuiPluginInstallConfiguration(source=str(source))
        )
        assert installed.plugin_id == "dev.ouro.tui_skill"
        assert "calendar" not in {skill.name for skill in (await controller.skill_catalog()).skills}

        assert (await controller.enable_plugin(installed.plugin_id)).status == "active"
        enabled_catalog = await controller.skill_catalog()
        plugin_skill = next(skill for skill in enabled_catalog.skills if skill.name == "calendar")
        assert plugin_skill.source == "plugin"
        assert plugin_skill.origin == "plugin"
        assert plugin_skill.extensions["ouro"]["plugin_id"] == installed.plugin_id
        assert (await controller.system_status()).skill_names == ("calendar",)

        with pytest.raises(ConfigError, match="managed through its owning plugin"):
            await controller.set_skill_enabled("calendar", enabled=False)

        assert (await controller.disable_plugin(installed.plugin_id)).status == "disabled"
        assert "calendar" not in {skill.name for skill in (await controller.skill_catalog()).skills}
        assert (await controller.system_status()).skill_names == ()
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_skill_toggle_changes_subsequent_dialogue_runs_in_same_controller(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill.\n---\n\nUse the demo workflow.\n",
        encoding="utf-8",
    )
    provider = _SkillPromptCapturingProvider()
    configuration = ConfigurationFacade(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=_skill_prompt_application(tmp_path, provider),
        configuration=configuration,
    )
    await controller.start()

    try:
        await controller.submit("first turn")
        assert "<name>demo</name>" in provider.system_prompts[-1]

        await controller.set_skill_enabled("demo", enabled=False)
        await controller.submit("second turn")
        assert "<name>demo</name>" not in provider.system_prompts[-1]

        await controller.set_skill_enabled("demo", enabled=True)
        await controller.submit("third turn")
        assert "<name>demo</name>" in provider.system_prompts[-1]
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_persists_queued_input_consumption_mode(tmp_path: Path) -> None:
    configuration = ConfigurationFacade(tmp_path)
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=_reasoning_application(tmp_path),
        configuration=configuration,
    )
    await controller.start()

    try:
        queued_inputs = await controller.queued_input_configuration()
        assert queued_inputs.consumption_mode == "one_by_one"

        queued_inputs = await controller.update_queued_input_consumption("all_at_once")

        assert queued_inputs.consumption_mode == "all_at_once"
        assert configuration.queued_input_config().consumption_mode == "all_at_once"
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


async def test_plugin_command_failure_keeps_created_tui_session_off_event_loop(
    tmp_path, monkeypatch
):
    application = _reasoning_application(tmp_path)
    controller = TuiController(lambda _event: None, lambda _status: None, application=application)
    await controller.start()
    loop_thread = threading.get_ident()
    create = application.runtime.create_session

    def checked_create(**kwargs):
        assert threading.get_ident() != loop_thread
        return create(**kwargs)

    class Plugins:
        def list_commands(self):
            return [
                PluginCommandView(
                    command_id="test:fail",
                    plugin_id="test",
                    name="fail",
                    requires_session=True,
                )
            ]

        async def execute_command(self, command_id, argument, *, session_id, workspace):
            assert controller.session_id == session_id
            raise ValueError("command failed after Session creation")

    application.plugins = Plugins()
    monkeypatch.setattr(application.runtime, "create_session", checked_create)
    try:
        with pytest.raises(ValueError, match="command failed after Session creation"):
            await controller.execute_plugin_command("fail", "preserve identity")
        assert controller.session_id is not None
        session = application.runtime.store.load_session_record(controller.session_id)
        assert session.title == "preserve identity"
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_submits_plugin_input_batch_as_one_run(tmp_path: Path) -> None:
    application = _reasoning_application(tmp_path)
    common = {
        "plugin_id": "dev.ouro.demo",
        "contribution_id": "goal",
        "generation_id": "generation_1",
        "execution_contract_hash": "contract_1",
    }

    class Plugins:
        def list_commands(self) -> list[PluginCommandView]:
            return [
                PluginCommandView(
                    command_id="dev.ouro.demo:goal",
                    plugin_id="dev.ouro.demo",
                    name="goal",
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
                output=f"goal {argument}",
                session_input_batches=[
                    AttributedSessionInputBatch(
                        inputs=(
                            AttributedSessionInput(
                                prompt="create",
                                role="user",
                                scaffold=True,
                                semantic_tag="goal-create",
                                **common,
                            ),
                            AttributedSessionInput(
                                prompt=argument,
                                role="user",
                                scaffold=False,
                                **common,
                            ),
                        )
                    )
                ],
            )

    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
    )
    await controller.start()
    application.plugins = Plugins()  # type: ignore[assignment]

    try:
        result = await controller.execute_plugin_command("goal", "ship")

        assert result is not None
        assert len(result.run_results) == 1
        assert controller.session_id is not None
        snapshot = application.runtime.store.load_session(controller.session_id)
        messages = [message for message in snapshot.messages if message.role == "user"]
        assert [message.text_content for message in messages] == ["create", "ship"]
        assert [message.scaffold for message in messages] == [True, False]
        assert [message.semantic_tag for message in messages] == ["goal-create", None]
        assert len({message.run_id for message in messages}) == 1
        added = [
            event
            for event in result.run_results[0].events
            if event.event_type == "plugin_message_added"
        ]
        assert [event.payload["message_id"] for event in added] == [
            message.message_id for message in messages
        ]
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_forks_current_session_without_mutating_source(
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
        source_before = application.runtime.store.load_session(source_session_id)

        branch = await controller.branch_session()
        source_after = application.runtime.store.load_session(source_session_id)
        loaded = await controller.load_session(branch.session.session_id)

        assert branch.session.parent_session_id == source_session_id
        assert [message.text_content for message in loaded.messages] == [
            message.text_content for message in source_before.messages
        ]
        assert source_after == source_before
        assert branch.source_message_id == source_before.messages[-1].message_id
        assert branch.session.forked_from_message_id == source_before.messages[-1].message_id
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
        original_snapshot = application.runtime.sessions.observation_snapshot

        def blocking_snapshot(session_id: str):
            reader_threads.append(threading.get_ident())
            entered.set()
            release.wait()
            return original_snapshot(session_id)

        monkeypatch.setattr(
            application.runtime.sessions,
            "observation_snapshot",
            blocking_snapshot,
        )
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
async def test_controller_event_ancestry_lookup_does_not_block_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[EventRecord] = []
    controller = TuiController(
        published.append,
        lambda _status: None,
        application=_reasoning_application(tmp_path),
    )
    await controller.start()

    try:
        controller._session_id = "ses_root"
        entered = threading.Event()
        release = threading.Event()
        lookup_threads: list[int] = []

        def blocking_lookup(_session_id: str) -> str:
            lookup_threads.append(threading.get_ident())
            entered.set()
            release.wait()
            return "ses_root"

        monkeypatch.setattr(controller, "_event_root_session_id", blocking_lookup)
        event = EventRecord(
            session_id="ses_child",
            run_id="run_child",
            event_type="approval_requested",
            sequence=1,
        )
        watchdog = threading.Timer(1, release.set)
        watchdog.start()
        event_loop_thread = threading.get_ident()
        receiving = asyncio.create_task(controller._receive_event(event))

        try:
            lookup_entered = await asyncio.to_thread(entered.wait, 2)
            event_loop_remained_responsive = not receiving.done()
        finally:
            release.set()
            watchdog.cancel()
        await receiving

        assert lookup_entered is True
        assert event_loop_remained_responsive is True
        assert lookup_threads != [event_loop_thread]
        assert published == [event]
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_observes_another_session_without_changing_control_target(
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
        controlled = await controller.submit("controlled conversation")
        observed = application.runtime.sessions.create(title="Observed child").session
        event = application.runtime.store.append_ordered_event(
            EventRecord(
                session_id=observed.session_id,
                run_id="run_observed",
                event_type="run_started",
                payload={"trigger": "delegation"},
            )
        )

        observation = await controller.observe_session(observed.session_id)

        assert controller.session_id == controlled.session.session_id
        assert observation.loaded.detail.session.session_id == observed.session_id
        assert observation.loaded.event_high_water == event.sequence
        observation.subscription.close()
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_selects_subagent_surface_under_root_control(
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
        root = application.runtime.sessions.create(title="Root").session
        child = SessionRecord(
            session_id="ses_direct_child",
            title="Direct child",
            session_kind="subagent",
            root_session_id=root.session_id,
            parent_session_id=root.session_id,
            delegation_id="dlg_direct_child",
        )
        application.runtime.store.create_session(child)

        loaded = await controller.load_session(child.session_id)

        assert loaded.detail.session.session_id == child.session_id
        assert controller.session_id == root.session_id
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_new_primary_session_cannot_be_claimed_by_concurrent_child_events(
    tmp_path: Path,
) -> None:
    application = _reasoning_application(tmp_path)
    published: list[EventRecord] = []
    controller = TuiController(
        published.append,
        lambda _status: None,
        application=application,
    )
    await controller.start()

    try:
        root = application.runtime.sessions.create(title="Root").session
        child = SessionRecord(
            title="Child",
            session_kind="subagent",
            root_session_id=root.session_id,
            parent_session_id=root.session_id,
            delegation_id="dlg_child",
        )
        application.runtime.store.create_session(child)
        controller._creating_session = True

        await controller._receive_event(
            EventRecord(
                session_id=child.session_id,
                run_id="run_child",
                event_type="model_text_delta",
                payload={"text": "child"},
                sequence=1,
            )
        )

        assert controller.session_id is None
        assert published == []

        root_event = EventRecord(
            session_id=root.session_id,
            run_id="run_root",
            event_type="run_started",
            payload={"trigger": "user"},
            sequence=1,
        )
        await controller._receive_event(root_event)

        assert controller.session_id is None
        assert published == []
        controller._bind_created_session(
            application.runtime.store.load_session_record(root.session_id)
        )
        await controller._receive_event(root_event)

        assert controller.session_id == root.session_id
        assert published == [root_event]
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_filters_child_stream_but_forwards_child_control_events(
    tmp_path: Path,
) -> None:
    application = _reasoning_application(tmp_path)
    published: list[EventRecord] = []
    controller = TuiController(
        published.append,
        lambda _status: None,
        application=application,
    )
    await controller.start()

    try:
        root = application.runtime.sessions.create(title="Root").session
        child = SessionRecord(
            title="Child",
            session_kind="subagent",
            root_session_id=root.session_id,
            parent_session_id=root.session_id,
            delegation_id="dlg_child",
        )
        application.runtime.store.create_session(child)
        controller._session_id = root.session_id
        delta = EventRecord(
            session_id=child.session_id,
            run_id="run_child",
            event_type="model_text_delta",
            payload={"text": "high-volume child stream"},
            sequence=1,
        )
        approval = EventRecord(
            session_id=child.session_id,
            run_id="run_child",
            event_type="approval_requested",
            payload={"tool_call_id": "call_1", "approval_id": "approval_1"},
            sequence=2,
        )

        await controller._receive_event(delta)
        await controller._receive_event(approval)

        assert published == [approval]
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_keeps_archive_policy_in_tui_and_unarchives_after_update(
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
        session = application.runtime.sessions.create(
            session_id="ses_archived_tui",
            title="Archived TUI session",
        ).session
        await controller.set_session_archived(session.session_id, archived=True)
        await controller.load_session(session.session_id)
        assert (
            next(
                item
                for item in await controller.sessions()
                if item.session_id == session.session_id
            ).archived
            is True
        )

        await controller.set_session_approval_preset("deny")
        assert (
            next(
                item
                for item in await controller.sessions()
                if item.session_id == session.session_id
            ).archived
            is False
        )

        await controller.set_session_archived(session.session_id, archived=True)
        await controller.submit("update archived session")
        assert (
            next(
                item
                for item in await controller.sessions()
                if item.session_id == session.session_id
            ).archived
            is False
        )

        first = application.runtime.sessions.create(
            session_id="ses_delete_first",
            title="Delete first",
        ).session
        second = application.runtime.sessions.create(
            session_id="ses_delete_second",
            title="Delete second",
        ).session
        await controller.set_session_archived(first.session_id, archived=True)
        await controller.set_session_archived(second.session_id, archived=True)
        removed = await controller.delete_sessions((first.session_id, second.session_id))
        assert set(removed) == {first.session_id, second.session_id}
        assert {item.session_id for item in await controller.sessions()} == {session.session_id}
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_session_catalog_only_projects_primary_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _reasoning_application(tmp_path)
    root = application.runtime.sessions.create(
        session_id="ses_catalog_root",
        title="Root",
    ).session
    child = SessionRecord(
        session_id="ses_catalog_child",
        title="Child",
        session_kind="subagent",
        root_session_id=root.session_id,
        parent_session_id=root.session_id,
        delegation_id="dlg_catalog_child",
    )
    application.runtime.store.create_session(child)
    assert application.runtime.store.refresh_catalog(child.session_id) is None
    index_path = application.runtime.store._catalog.index_path
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    root_entry = next(item for item in payload["sessions"] if item["session_id"] == root.session_id)
    root_entry.pop("session_kind")
    index_path.write_text(json.dumps(payload), encoding="utf-8")
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
    )
    await controller.start()

    def unexpected_canonical_load(_session_id: str) -> SessionRecord:
        raise AssertionError("classified catalog entries must not reload canonical Sessions")

    def unexpected_canonical_scan() -> list[SessionRecord]:
        raise AssertionError("TUI session menus must only read the catalog")

    monkeypatch.setattr(
        application.runtime.store,
        "load_session_record",
        unexpected_canonical_load,
    )
    monkeypatch.setattr(
        application.runtime.store,
        "list_sessions",
        unexpected_canonical_scan,
    )

    try:
        entries = await controller.sessions()
        assert [entry.session_id for entry in entries] == [root.session_id]
        assert entries[0].session_kind is None
        assert entries[0].health is None
        assert entries[0].catalog_error is not None
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

        async def unexpected_reconciliation(_session_id: str) -> None:
            raise AssertionError("opening history must not reconcile Runtime execution state")

        def unexpected_event_page(*_args, **_kwargs):
            raise AssertionError("completed Sessions must not replay the event journal")

        monkeypatch.setattr(application.runtime, "reconcile_session", unexpected_reconciliation)
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

        await controller.new_session()
        assert await controller.session_approval_preset() == "inherit"
        assert (await controller.session_reasoning()).requested == ReasoningRequest()
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_controller_resets_reasoning_only_when_model_route_changes(
    tmp_path: Path,
) -> None:
    application = _reasoning_application(tmp_path)
    runtime = application.runtime
    original_key = runtime.current_model().key
    runtime.models = StaticRouteCatalog(
        {
            original_key: runtime.adapter,
            "other/limited": RuntimeAdapter(
                FauxProvider(),
                ModelMetadata(provider_id="other", endpoint_id="limited", model_id="limited-model"),
            ),
        },
        default_key=original_key,
    )
    controller = TuiController(
        lambda _event: None,
        lambda _status: None,
        application=application,
    )
    await controller.start()

    try:
        await controller.set_session_reasoning(ReasoningRequest(mode="enabled", effort="high"))
        result = await controller.submit("create configured session")
        await controller.select_model(original_key)
        assert (await controller.session_reasoning()).requested == result.session.reasoning_request

        await controller.select_model("other/limited")
        assert (await controller.session_reasoning()).requested == ReasoningRequest()
        assert runtime.sessions.open(result.session.session_id).session.current_model == (
            "other/limited"
        )
    finally:
        await controller.close()
        await application.close()
