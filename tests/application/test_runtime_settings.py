from __future__ import annotations

import pytest

from ouro.application.runtime.service import RuntimeService
from ouro.config.compaction import CompactionConfig, CompactionConfigManager, CompactionModelConfig
from ouro.config.file import JsonConfigFile
from ouro.config.permissions import PermissionConfigManager
from ouro.config.queued_inputs import QueuedInputConfigManager
from ouro.config.run_budget import RunBudgetConfigManager
from ouro.config.runtime import RuntimeSettingsResolver
from ouro.config.tool_execution import ToolExecutionConfigManager
from ouro.core.adapter import RuntimeAdapter
from ouro.core.compaction import CompactionPolicy, CompactionStrategyRegistry
from ouro.core.errors import CompactionError
from ouro.core.provider import ModelMetadata
from ouro.core.run_budget import RunBudgetPolicy
from ouro.local_application import build_local_runtime
from ouro.sessions import LocalSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult
from tests.application.helpers import running_runtime_task
from tests.application.test_compaction import SummaryRouteProvider, _append_turns, _summary_adapter
from tests.application.test_queued_input_modes import _runtime as queue_runtime
from tests.application.test_queued_input_modes import _user_texts
from tests.application.test_run_compaction import RecordingSummary, ToolLoopProvider
from tests.event_capture import recorded_events


def loop_runtime(tmp_path, handler, *, rounds=3):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    provider = ToolLoopProvider(rounds=rounds)
    tools = ToolRegistry()
    tools.register(
        Tool(
            definition=ToolDefinition(name="ReadChunk", description="Read.", effect="read_only"),
            handler=handler,
        )
    )
    strategy = RecordingSummary()
    strategies = CompactionStrategyRegistry()
    strategies.register(strategy)
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(
                model_id="loop",
                provider_id=provider.provider_id,
                context_window=3000,
                max_output_tokens=500,
            ),
        ),
        tools=tools,
        system_prompt="",
        compaction_strategies=strategies,
        settings_resolver=RuntimeSettingsResolver(tmp_path).snapshot,
    )
    return runtime, provider, strategy


@pytest.mark.parametrize("change", ["lower", "raise", "disable", "enable"])
async def test_compaction_policy_changes_between_rounds(tmp_path, change):
    manager = CompactionConfigManager(tmp_path)
    manager.save(
        CompactionConfig(
            strategy_id=RecordingSummary.strategy_id,
            policy=CompactionPolicy(
                auto_enabled=change != "enable",
                trigger_ratio=0.95 if change == "lower" else 0.2,
                target_ratio=0.1,
            ),
        )
    )

    def read(invocation, context):
        if invocation.tool_call_id == "read_1":
            manager.save(
                CompactionConfig(
                    strategy_id=RecordingSummary.strategy_id,
                    policy=CompactionPolicy(
                        auto_enabled=change != "disable",
                        trigger_ratio=0.95 if change == "raise" else 0.2,
                        target_ratio=0.05,
                    ),
                )
            )
        return ToolResult(
            tool_call_id=invocation.tool_call_id, tool_name=invocation.tool_name, content="x" * 1200
        )

    runtime, provider, strategy = loop_runtime(tmp_path, read)
    async with runtime:
        result = await runtime.run("Read chunks")
        assert result.run.status == "completed", result.run.error
        assert len(provider.calls) == 4
        assert bool(strategy.requests) == (change in {"lower", "enable"})
        events = recorded_events(result.session.session_id)
        started = [event for event in events if event.event_type == "compaction_started"]
        assert all(event.payload["target_tokens"] == 125 for event in started)


async def test_live_budget_can_pause_and_resume_same_run(tmp_path):
    budgets = RunBudgetConfigManager(tmp_path)

    def read(invocation, context):
        budgets.save(RunBudgetPolicy(max_model_rounds=1))
        return ToolResult(
            tool_call_id=invocation.tool_call_id, tool_name=invocation.tool_name, content="done"
        )

    runtime, provider, _ = loop_runtime(tmp_path, read, rounds=1)
    async with runtime:
        result = await runtime.run("Read once")
        assert result.run.status == "paused"
        assert len(provider.calls) == 1
        assert result.run.metadata["run_budget"]["max_model_rounds"] == 1
        budgets.save(RunBudgetPolicy(max_model_rounds=2))
        continued = await runtime.continue_run(session_id=result.session.session_id)
        assert continued.run.status == "completed", continued.run.error
        assert continued.run.run_id == result.run.run_id
        assert len(provider.calls) == 2


async def test_tool_batches_pick_up_execution_settings_and_permissions(tmp_path):
    execution = ToolExecutionConfigManager(tmp_path)
    permissions = PermissionConfigManager(tmp_path)
    observed = []

    async def read(invocation, context):
        observed.append(context.max_output_chars)
        if len(observed) == 1:
            execution.update(max_output_chars=2048, mode="parallel", max_concurrency=2)
            await runtime.refresh_configuration()
            # The active tool retains the context with which it was invoked.
            assert context.max_output_chars == 100_000
        else:
            permissions.set_tool_mode("ReadChunk", "deny")
        return ToolResult(
            tool_call_id=invocation.tool_call_id, tool_name=invocation.tool_name, content="done"
        )

    runtime, provider, _ = loop_runtime(tmp_path, read)
    async with runtime:
        result = await runtime.run("Read three times")
        assert result.run.status == "completed", result.run.error
        assert len(provider.calls) == 4
        assert observed == [100_000, 2048]
        results = [m.tool_result for m in provider.calls[-1] if m.tool_result]
        assert results[-1].is_error


async def test_queue_mode_changes_while_model_is_running(tmp_path):
    store, provider, runtime = queue_runtime(tmp_path, consumption_mode="one_by_one")
    runtime.settings_resolver = RuntimeSettingsResolver(tmp_path).snapshot
    async with running_runtime_task(runtime, runtime.run("initial")) as task:
        await provider.first_started.wait()
        session = store.list_sessions()[0]
        await runtime.run("steer-1", session_id=session.session_id, queue_mode="steer")
        await runtime.run("steer-2", session_id=session.session_id, queue_mode="steer")
        QueuedInputConfigManager(tmp_path).update(consumption_mode="all_at_once")
        provider.release_first.set()
        result = await task
        assert result.run.status == "completed"
        assert [_user_texts(messages) for messages in provider.contexts] == [
            ["initial"],
            ["initial", "steer-1", "steer-2"],
        ]


async def test_manual_compaction_uses_current_model_route_and_parameters(tmp_path):
    manager = CompactionConfigManager(tmp_path)
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    primary = SummaryRouteProvider()
    dedicated = SummaryRouteProvider()
    runtime = RuntimeService(
        store=store,
        adapter=_summary_adapter(primary, provider_id="runtime", endpoint_id="chat"),
        tools=ToolRegistry(),
        system_prompt="",
        settings_resolver=RuntimeSettingsResolver(tmp_path).snapshot,
        compaction_adapter_resolver=lambda model: _summary_adapter(
            dedicated, provider_id=model.provider_id, endpoint_id=model.endpoint_id
        ),
    )
    async with runtime:
        session = runtime.create_session(title="Live summary")
        _append_turns(store, session.session_id, 3)
        manager.save(
            CompactionConfig(
                model=CompactionModelConfig(
                    provider_id="dedicated",
                    endpoint_id="summary",
                    temperature=0.3,
                    max_output_tokens=512,
                )
            )
        )
        result = await runtime.compact(session_id=session.session_id)
        assert result.compaction is not None
        assert not primary.models
        assert dedicated.models[-1].temperature == 0.3
        assert dedicated.models[-1].max_output_tokens == 512
        manager.save(CompactionConfig(model=CompactionModelConfig(temperature=0.7)))
        _append_turns(store, session.session_id, 3, offset=3)
        result = await runtime.compact(session_id=session.session_id)
        assert result.compaction is not None
        assert primary.models[-1].temperature == 0.7
        assert primary.models[-1].max_output_tokens is None


async def test_compaction_keeps_policy_and_fallback_during_configuration_update(tmp_path):
    manager = CompactionConfigManager(tmp_path)
    runtime, _, fallback = loop_runtime(tmp_path, lambda *_: None)

    class ChangingStrategy:
        strategy_id = "test.changing"
        version = "1"

        async def compact(self, request, *, signal=None):
            manager.save(
                CompactionConfig(
                    strategy_id=fallback.strategy_id,
                    policy=CompactionPolicy(trigger_ratio=0.8, target_ratio=0.3),
                )
            )
            await runtime.refresh_configuration()
            raise CompactionError("use the original fallback")

    runtime.compaction_strategies.register(ChangingStrategy())
    manager.save(
        CompactionConfig(
            strategy_id=ChangingStrategy.strategy_id,
            fallback_strategy_id=fallback.strategy_id,
            policy=CompactionPolicy(trigger_ratio=0.8, target_ratio=0.1),
        )
    )
    async with runtime:
        session = runtime.create_session(title="Stable compaction")
        _append_turns(runtime.store, session.session_id, 3)
        result = await runtime.compact(session_id=session.session_id)
        assert result.compaction is not None
        assert result.compaction.strategy_id == fallback.strategy_id
        _append_turns(runtime.store, session.session_id, 3, offset=3)
        result = await runtime.compact(session_id=session.session_id)
        assert result.compaction is not None
        events = recorded_events(session.session_id)
        targets = [
            event.payload["target_tokens"]
            for event in events
            if event.event_type == "compaction_started"
        ]
        assert targets == [250, 750]
        assert len(fallback.requests) == 2


async def test_invalid_configuration_stops_execution_until_corrected(tmp_path):
    config = JsonConfigFile(tmp_path)

    def read(invocation, context):
        config.write({"compaction": {"policy": {"trigger_ratio": 0}}})
        return ToolResult(
            tool_call_id=invocation.tool_call_id, tool_name=invocation.tool_name, content="done"
        )

    runtime, provider, _ = loop_runtime(tmp_path, read, rounds=1)
    async with runtime:
        result = await runtime.run("Read once")
        assert result.run.status == "failed"
        assert "configuration" in result.run.error
        assert len(provider.calls) == 1
        config.write({})
        continued = await runtime.run("Continue", session_id=result.session.session_id)
        assert continued.run.status == "completed", continued.run.error
        assert len(provider.calls) == 2


async def test_local_composition_uses_saved_settings_on_existing_runtime(tmp_path):
    runtime = build_local_runtime(tmp_path)
    async with runtime:
        RunBudgetConfigManager(tmp_path).save(RunBudgetPolicy(max_model_rounds=5))
        first = await runtime.run("Hello")
        assert first.run.status == "completed", first.run.error
        assert first.run.metadata["run_budget"]["max_model_rounds"] == 5
        RunBudgetConfigManager(tmp_path).save(RunBudgetPolicy(max_model_rounds=2))
        second = await runtime.run("Again", session_id=first.session.session_id)
        assert second.run.status == "completed", second.run.error
        assert second.run.metadata["run_budget"]["max_model_rounds"] == 2


async def test_empty_queue_does_not_add_configuration_refreshes(tmp_path, monkeypatch):
    def read(invocation, context):
        return ToolResult(
            tool_call_id=invocation.tool_call_id, tool_name=invocation.tool_name, content="done"
        )

    runtime, provider, _ = loop_runtime(tmp_path, read)
    resolver = RuntimeSettingsResolver(tmp_path)
    refreshes = []
    reads = []
    original_read = resolver.file.read

    def snapshot():
        refreshes.append(True)
        return resolver.snapshot()

    def read_config():
        reads.append(True)
        return original_read()

    runtime.settings_resolver = snapshot
    monkeypatch.setattr(resolver.file, "read", read_config)
    async with runtime:
        result = await runtime.run("Read three times")
        assert result.run.status == "completed", result.run.error
        # One initial binding, then the model requests and tool batches that consume settings.
        assert len(refreshes) <= 1 + len(provider.calls) + 3
        assert len(reads) == 1


async def test_exhausted_budget_does_not_bind_capabilities(tmp_path, monkeypatch):
    def read(invocation, context):
        RunBudgetConfigManager(tmp_path).save(RunBudgetPolicy(max_model_rounds=1))
        return ToolResult(
            tool_call_id=invocation.tool_call_id, tool_name=invocation.tool_name, content="done"
        )

    runtime, _, _ = loop_runtime(tmp_path, read, rounds=1)
    bindings = []
    original = runtime._bind_run_capabilities

    async def bind(**kwargs):
        bindings.append(kwargs["model_round"])
        return await original(**kwargs)

    monkeypatch.setattr(runtime, "_bind_run_capabilities", bind)
    async with runtime:
        result = await runtime.run("Read once")
        assert result.run.status == "paused"
        assert bindings == [1]
        assert result.run.metadata["run_budget"]["max_model_rounds"] == 1


async def test_configuration_publication_preserves_other_runs_coordinators(tmp_path):
    runtime, _, _ = loop_runtime(tmp_path, lambda *_: None)
    async with runtime:
        await runtime.refresh_configuration()
        first = runtime._tool_calls_for_run("first")
        second = runtime._tool_calls_for_run("second")
        PermissionConfigManager(tmp_path).set_tool_mode("ReadChunk", "deny")
        await runtime.refresh_configuration()
        assert runtime._run_resources["first"].tool_calls is first
        assert runtime._run_resources["second"].tool_calls is second
        updated = runtime._tool_calls_for_run("first")
        assert updated.executor.permission_overrides["ReadChunk"] == "deny"
        assert "ReadChunk" not in second.executor.permission_overrides
        assert runtime._tool_calls_for_run("first") is updated


async def test_unchanged_budget_does_not_write_policy_again(tmp_path, monkeypatch):
    def read(invocation, context):
        RunBudgetConfigManager(tmp_path).save(RunBudgetPolicy(max_model_rounds=10))
        return ToolResult(
            tool_call_id=invocation.tool_call_id, tool_name=invocation.tool_name, content="done"
        )

    runtime, _, _ = loop_runtime(tmp_path, read)
    writes = []
    save = runtime._save_run_budget

    def record_write(run, policy):
        writes.append(policy)
        save(run, policy)

    monkeypatch.setattr(runtime, "_save_run_budget", record_write)
    async with runtime:
        result = await runtime.run("Read three times")
        assert result.run.status == "completed", result.run.error
        assert len(writes) == 1
        assert writes[0]["max_model_rounds"] == 10
