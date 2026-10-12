from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from ouro.application.plugins import PluginService
from ouro.application.runtime import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.observers import ObserverRegistry
from ouro.core.provider import ModelMetadata, ProviderStreamEvent, ToolCallDelta
from ouro.plugins.contributions import ContributionCoordinator
from ouro.plugins.runtime import PluginRuntimeResource
from ouro.plugins.store import PluginPackageStore
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult

PLUGIN = "dev.ouro.examples.review_driver"
DRIVER = f"{PLUGIN}/run_drivers/review"
EXAMPLE = Path(__file__).parents[2] / "examples/plugins/review-driver"


def plugin_source(tmp_path, kind):
    package = tmp_path / "package"
    shutil.copytree(EXAMPLE, package)
    manifest = package / ".ouro-plugin/plugin.json"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    if kind == "worker":
        value["runtime"] = {
            "kind": "worker",
            "command": ["{python}", "worker.py"],
            "protocol": "3.0",
        }
    manifest.write_text(json.dumps(value), encoding="utf-8")
    return package


def set_driver_body(package: Path, body: str) -> None:
    source = package / "review_driver_plugin.py"
    text = source.read_text(encoding="utf-8")
    start = text.index("async def review(")
    end = text.index("class ReviewPlugin:")
    source.write_text(text[:start] + body + "\n\n" + text[end:], encoding="utf-8")


def host(tmp_path, store, calls, mode="approval", content="deployed", handler=None):
    tools = ToolRegistry()

    def execute(invocation, context):
        calls.append(invocation.tool_call_id)
        return ToolResult(tool_call_id=invocation.tool_call_id, tool_name="Deploy", content=content)

    tools.register(
        Tool(
            definition=ToolDefinition(name="Deploy", description="Deploy", permission_mode=mode),
            handler=handler or execute,
        )
    )
    plugin_runtime = PluginRuntimeResource(
        PluginPackageStore(tmp_path / "home"),
        ContributionCoordinator(tools=tools, observers=ObserverRegistry()),
    )
    service = PluginService(plugin_runtime.store, runtime=plugin_runtime, session_store=store)
    provider = FauxProvider(
        event_batches=[
            [
                ProviderStreamEvent(type="text_delta", text="reviewed"),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(
                    type="tool_call_delta",
                    tool_call_delta=ToolCallDelta(
                        index=0, id_delta="deploy", name_delta="Deploy", arguments_delta="{}"
                    ),
                ),
                ProviderStreamEvent(type="done"),
            ],
        ]
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(provider, ModelMetadata(model_id="faux", provider_id="faux")),
        tools=tools,
    )
    runtime.bind_extensions(catalog=plugin_runtime)
    return runtime, plugin_runtime, service


@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
async def test_driver_wait_restarts_with_exact_contract_native_results_and_checkpoint(
    tmp_path, store_type, kind
):
    store = store_type(
        tmp_path / "sessions.sqlite3" if store_type is SQLiteSessionStore else tmp_path / "sessions"
    )
    store.initialize()
    calls = []
    runtime, plugins, service = host(tmp_path, store, calls)
    service.install(plugin_source(tmp_path, kind))
    assert (await service.enable(PLUGIN)).status == "active", service.show(PLUGIN)
    try:
        pending = await runtime.run("deploy", driver_id=DRIVER)
        assert pending.run.status == "waiting_approval", pending.run.error
        durable = store.load_run(pending.session.session_id, pending.run.run_id)
        assert durable.driver.driver_id == DRIVER
        assert durable.driver_checkpoint.payload == {"step": "summarize", "review": "reviewed"}
        assert durable.driver_checkpoint.batch_id == durable.active_tool_batch.batch_id
        assert durable.model_round_count == 2
        assert calls == []
        assert plugins.catalog().lease_count == 0
    finally:
        await runtime.close()
        await plugins.close()

    runtime, plugins, service = host(tmp_path, store, calls)
    await plugins.start()
    try:
        result = await runtime.approve_tool(
            session_id=pending.session.session_id,
            run_id=pending.run.run_id,
            approval_id=pending.run.pending_approval_id,
            approved=True,
        )
        assert result.run.status == "completed", result.run.error
        assert result.final_text == "deployed"
        assert result.run.model_round_count == 2
        assert calls == ["deploy"]
        assert result.run.driver == pending.run.driver
        assert result.run.driver_checkpoint.state == "consumed"
        assert result.run.active_tool_batch is None
        history = store.load_runtime_session(result.session.session_id)
        assert [
            message.tool_result.tool_call_id for message in history.messages if message.tool_result
        ] == ["deploy"]
    finally:
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
async def test_driver_immediate_tools_consume_checkpoint_without_parking(tmp_path, kind):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    calls = []
    runtime, plugins, service = host(tmp_path, store, calls, mode="auto")
    service.install(plugin_source(tmp_path, kind))
    assert (await service.enable(PLUGIN)).status == "active"
    try:
        result = await runtime.run("deploy", driver_id=DRIVER)
        assert result.run.status == "completed", result.run.error
        assert result.final_text == "deployed"
        assert result.run.driver_checkpoint.state == "consumed"
        assert calls == ["deploy"]
    finally:
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("after_consume", [False, True])
async def test_checkpoint_crash_boundary_allows_only_unconsumed_native_results(
    tmp_path, monkeypatch, after_consume
):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    calls = []
    runtime, plugins, service = host(tmp_path, store, calls)
    service.install(plugin_source(tmp_path, "python-inprocess"))
    assert (await service.enable(PLUGIN)).status == "active"
    try:
        pending = await runtime.run("deploy", driver_id=DRIVER)
        consume = runtime.run_lifecycle.consume_driver_checkpoint

        def interrupted(session_id, run_id):
            if after_consume:
                consume(session_id, run_id)
            raise RuntimeError("simulated interruption at checkpoint consumption")

        monkeypatch.setattr(runtime.run_lifecycle, "consume_driver_checkpoint", interrupted)
        failed = await runtime.approve_tool(
            session_id=pending.session.session_id,
            run_id=pending.run.run_id,
            approval_id=pending.run.pending_approval_id,
            approved=True,
        )
        assert failed.run.status == "failed"
        saved = store.load_run(pending.session.session_id, pending.run.run_id)
        assert saved.driver_checkpoint.state == ("consumed" if after_consume else "ready")
        monkeypatch.setattr(runtime.run_lifecycle, "consume_driver_checkpoint", consume)
        result = await runtime.continue_run(session_id=pending.session.session_id)
        assert result.run.status == ("failed" if after_consume else "completed"), result.run.error
        assert result.run.model_round_count == 2
        assert calls == ["deploy"]
        if after_consume:
            assert "checkpoint" in result.run.error
        else:
            assert result.final_text == "deployed"
    finally:
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
@pytest.mark.parametrize("restart", [False, True])
async def test_driver_wait_uses_retained_implementation_after_upgrade(tmp_path, kind, restart):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    calls = []
    runtime, plugins, service = host(tmp_path, store, calls)
    package = plugin_source(tmp_path, kind)
    service.install(package)
    assert (await service.enable(PLUGIN)).status == "active"
    try:
        pending = await runtime.run("deploy", driver_id=DRIVER)
        manifest = package / ".ouro-plugin/plugin.json"
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["version"] = "0.1.1"
        manifest.write_text(json.dumps(value), encoding="utf-8")
        set_driver_body(
            package,
            'async def review(context, resume):\n    return sdk.DriverFinished("replacement")\n',
        )
        installed = service.install(package)
        assert (await service.select(PLUGIN, installed.package_digest)).status == "active"
        await plugins.wait_stopped(PLUGIN)
        original_release = pending.run.driver.implementation.release_id
        removed = await service.remove(PLUGIN, package_digest=original_release)
        assert removed.retained_by == [f"{pending.session.session_id}/{pending.run.run_id}"]
        service.install(plugins.store.package_path(PLUGIN, original_release))
        if restart:
            await runtime.close()
            await plugins.close()
            runtime, plugins, service = host(tmp_path, store, calls)
            await plugins.start()
        result = await runtime.approve_tool(
            session_id=pending.session.session_id,
            run_id=pending.run.run_id,
            approval_id=pending.run.pending_approval_id,
            approved=True,
        )
        assert result.run.status == "completed", result.run.error
        assert result.final_text == "deployed"
        assert result.run.driver == pending.run.driver
        assert result.run.driver_checkpoint.state == "consumed"
        assert result.run.model_round_count == 2
        assert calls == ["deploy"]
        assert plugins.store.read_state().plugins[PLUGIN].selected().package_digest == (
            installed.package_digest
        )
        assert not plugins._retired
        fresh = await runtime.run("new work", driver_id=DRIVER)
        assert fresh.final_text == "replacement", fresh.run.error
    finally:
        await runtime.close()
        await plugins.close()


async def test_driver_cannot_leave_a_native_operation_running_after_return(tmp_path):
    from ouro.application.run_drivers import NativeRunDriverContext
    from ouro.core.cancellation import AbortSignal
    from ouro.core.plugin_contracts import PluginImplementation
    from ouro.core.run_drivers import DriverFinished, RunDriverBinding
    from ouro.core.state import RunRecord, SessionRecord

    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, _ = host(tmp_path, store, [])
    session = SessionRecord()
    store.create_session(session)
    run = RunRecord(
        session_id=session.session_id,
        driver=RunDriverBinding(
            driver_id=DRIVER,
            contract_hash="entry",
            implementation=PluginImplementation(
                plugin_id=PLUGIN,
                plugin_class="user",
                release_id="release",
                configuration_revision_id="configuration",
                execution_contract_hash="execution",
            ),
        ),
    )
    session, run = runtime.run_lifecycle.start(
        session=session, run=run, snapshot=store.load_runtime_session(session.session_id)
    )
    entered, closed = asyncio.Event(), asyncio.Event()

    async def native_request(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def unused_tools(request):
        raise AssertionError("unexpected tools")

    async def driver(context, resume):
        asyncio.create_task(context.request_model())
        await entered.wait()
        return DriverFinished("escaped")

    context = NativeRunDriverContext(
        session=session,
        run=run,
        messages=[],
        sequence=0,
        emitted=[],
        abort_signal=AbortSignal(),
        request_model=native_request,
        request_tools=unused_tools,
        request_driver_tools=unused_tools,
        consume_inputs=runtime._consume_driver_inputs,
        runs=runtime.run_lifecycle,
    )
    try:
        result = await context.invoke(driver)
        assert closed.is_set()
        assert result.run.status == "failed"
        assert "await" in result.run.error
    finally:
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
@pytest.mark.parametrize("stop", ["abort", "disable"])
async def test_driver_cancellation_closes_native_model_stream_and_invocation(
    tmp_path, monkeypatch, kind, stop
):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    service.install(plugin_source(tmp_path, kind))
    assert (await service.enable(PLUGIN)).status == "active"
    entered, closed = asyncio.Event(), asyncio.Event()
    sessions = []

    async def streaming(**kwargs):
        entered.set()
        try:
            yield ProviderStreamEvent(type="text_delta", text="partial")
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(runtime.adapter.provider, "stream", streaming)
    task = asyncio.create_task(
        runtime.run("review", driver_id=DRIVER, on_session_created=sessions.append)
    )
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            if stop == "abort":
                await runtime.abort_run(session_id=sessions[0].session_id)
            else:
                assert (await service.disable(PLUGIN)).status == "disabled"
            result = await task
        assert result.run.status == ("aborted" if stop == "abort" else "failed"), result.run.error
        assert closed.is_set()
        assert not store.is_session_locked(result.session.session_id)
        assert plugins.catalog().lease_count == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()
        await plugins.close()


async def test_sdk_driver_model_budget_cannot_be_bypassed(tmp_path):
    from ouro.core.run_budget import RunBudgetPolicy

    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    calls = []
    runtime, plugins, service = host(tmp_path, store, calls)
    runtime.run_budget = RunBudgetPolicy(max_model_rounds=1)
    service.install(plugin_source(tmp_path, "python-inprocess"))
    assert (await service.enable(PLUGIN)).status == "active"
    try:
        result = await runtime.run("deploy", driver_id=DRIVER)
        assert result.run.status == "paused", result.run.error
        assert result.run.model_round_count == 1
        assert calls == []
    finally:
        await runtime.close()
        await plugins.close()


async def test_disabled_driver_keeps_wait_results_available_without_default_fallback(tmp_path):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    calls = []
    runtime, plugins, service = host(tmp_path, store, calls)
    service.install(plugin_source(tmp_path, "python-inprocess"))
    assert (await service.enable(PLUGIN)).status == "active"
    try:
        pending = await runtime.run("deploy", driver_id=DRIVER)
        assert (await service.disable(PLUGIN)).status == "disabled"
        assert not plugins.has_resources(PLUGIN)
        result = await runtime.approve_tool(
            session_id=pending.session.session_id,
            run_id=pending.run.run_id,
            approval_id=pending.run.pending_approval_id,
            approved=True,
        )
        assert result.run.status == "failed"
        assert "driver implementation is disabled" in result.run.error
        assert result.run.driver_checkpoint.state == "ready"
        assert result.run.model_round_count == 2
        assert (await service.enable(PLUGIN)).status == "active"
        result = await runtime.continue_run(session_id=pending.session.session_id)
        assert result.run.status == "completed", result.run.error
        assert calls == ["deploy"]
    finally:
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
async def test_model_cannot_call_tools_excluded_by_the_driver_request(tmp_path, kind):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    calls = []
    runtime, plugins, service = host(tmp_path, store, calls, mode="auto")
    runtime.adapter.provider = FauxProvider(
        [
            ProviderStreamEvent(
                type="tool_call_delta",
                tool_call_delta=ToolCallDelta(
                    index=0, id_delta="deploy", name_delta="Deploy", arguments_delta="{}"
                ),
            ),
            ProviderStreamEvent(type="done"),
        ]
    )
    service.install(plugin_source(tmp_path, kind))
    assert (await service.enable(PLUGIN)).status == "active"
    try:
        result = await runtime.run("deploy", driver_id=DRIVER)
        assert result.run.status == "failed"
        assert "selected tools" in result.run.error
        assert result.run.model_round_count == 1
        assert calls == []
    finally:
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
async def test_programmatic_tool_wait_preserves_driver_provenance_without_model_calls(
    tmp_path, store_type, kind
):
    from ouro.core.context import ContextBuilder
    from ouro.core.message_frames import project_tool_exchanges

    store = store_type(
        tmp_path / "sessions.sqlite3" if store_type is SQLiteSessionStore else tmp_path / "sessions"
    )
    store.initialize()
    calls = []
    runtime, plugins, service = host(tmp_path, store, calls)
    package = plugin_source(tmp_path, kind)
    set_driver_body(
        package,
        """async def review(context, resume):
    if resume is None:
        result = await context.tools.execute(
            (sdk.RunToolCall("Deploy"),), checkpoint={"stage": "finish"}
        )
        if result.suspended:
            return sdk.DriverSuspended()
        results = result.results
    else:
        assert resume.checkpoint == {"stage": "finish"}
        results = resume.results
    return sdk.DriverFinished(results[0].content)
""",
    )
    service.install(package)
    assert (await service.enable(PLUGIN)).status == "active"
    try:
        pending = await runtime.run("deploy", driver_id=DRIVER)
        assert pending.run.status == "waiting_approval", pending.run.error
        batch = pending.run.active_tool_batch
        assert batch.producer.kind == "driver"
        assert batch.producer.driver_id == DRIVER
        assert batch.assistant_message_id is None
        assert batch.calls[0].invocation.producer == batch.producer
        assert pending.run.model_round_count == 0
        assert not any(
            message.tool_calls
            for message in store.load_runtime_session(pending.session.session_id).messages
        )
    finally:
        await runtime.close()
        await plugins.close()

    runtime, plugins, _ = host(tmp_path, store, calls)
    await plugins.start()
    try:
        result = await runtime.approve_tool(
            session_id=pending.session.session_id,
            run_id=pending.run.run_id,
            approval_id=pending.run.pending_approval_id,
            approved=True,
        )
        assert result.run.status == "completed", result.run.error
        assert result.final_text == "deployed"
        assert result.run.model_round_count == 0
        assert len(calls) == 1
        history = store.load_runtime_session(result.session.session_id)
        receipt = next(item for item in history.messages if item.tool_result is not None)
        assert receipt.tool_result.producer == batch.producer
        assert receipt.scaffold
        projected, excluded = project_tool_exchanges(history.messages)
        assert excluded == ()
        view = ContextBuilder().build(messages=projected, tools=[])
        assert not any(item.tool_calls for item in view.model_messages)
        native_output = next(item for item in view.model_messages if item.tool_result is not None)
        assert native_output.tool_result.producer == batch.producer
        assert "deployed" in native_output.text_content
    finally:
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("programmatic", [False, True])
@pytest.mark.parametrize("canonical_only", [False, True])
async def test_result_commit_interruption_recovers_checkpoint_without_repeating_tools(
    tmp_path, monkeypatch, programmatic, canonical_only
):
    from ouro.core.content import ImageContent, TextContent

    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    calls = []
    media = ImageContent(uri="data:image/png;base64,aW1hZ2U=", media_type="image/png")
    content = [TextContent(text="ok"), media]
    runtime, plugins, service = host(tmp_path, store, calls, mode="auto", content=content)
    package = plugin_source(tmp_path, "python-inprocess")
    if programmatic:
        set_driver_body(
            package,
            """async def review(context, resume):
    if resume is None:
        await context.tools.execute((sdk.RunToolCall("Deploy"),), checkpoint={"stage": "finish"})
        return sdk.DriverFinished("done")
    assert resume.checkpoint == {"stage": "finish"}
    return sdk.DriverFinished("recovered")
""",
        )
    service.install(package)
    await service.enable(PLUGIN)
    save = runtime.run_lifecycle.save_batch_state

    def interrupted(**kwargs):
        if kwargs["batch"] is not None and kwargs["batch"].phase == "completed":
            raise RuntimeError("interrupted after canonical tool result commit")
        return save(**kwargs)

    monkeypatch.setattr(runtime.run_lifecycle, "save_batch_state", interrupted)
    try:
        failed = await runtime.run("deploy", driver_id=DRIVER)
        assert failed.run.status == "failed", failed.run.error
        assert len(calls) == 1
        saved = store.load_run(failed.session.session_id, failed.run.run_id)
        assert saved.driver_checkpoint.state == "pending"
        assert saved.active_tool_batch.phase == "committing_results"
        if canonical_only:
            # Exercise the canonical receipt as the surviving durable evidence.
            batch = saved.active_tool_batch
            store.save_run(
                saved.model_copy(
                    update={
                        "active_tool_batch": batch.model_copy(
                            update={
                                "calls": [
                                    call.model_copy(update={"result": None}) for call in batch.calls
                                ]
                            }
                        )
                    }
                )
            )
        monkeypatch.setattr(runtime.run_lifecycle, "save_batch_state", save)
        reconciled = await runtime.run_recovery.reconcile(
            store.load_runtime_session(failed.session.session_id)
        )
        repaired = next(item for item in reconciled.runs if item.run_id == saved.run_id)
        assert repaired.driver_checkpoint.state == "ready"
        assert repaired.active_tool_batch.calls[0].status == "completed"
        if canonical_only:
            assert repaired.active_tool_batch.calls[0].result.content == [
                TextContent(text="ok"),
                media,
            ]
        result = await runtime.continue_run(session_id=failed.session.session_id)
        assert result.run.status == "completed", result.run.error
        assert len(calls) == 1
        assert result.run.driver_checkpoint.state == "consumed"
    finally:
        await runtime.close()
        await plugins.close()


async def test_programmatic_unknown_outcome_is_delivered_without_repeating_side_effect(
    tmp_path, monkeypatch
):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    calls = []
    runtime, plugins, service = host(tmp_path, store, calls, mode="auto")
    package = plugin_source(tmp_path, "worker")
    set_driver_body(
        package,
        """async def review(context, resume):
    if resume is None:
        await context.tools.execute((sdk.RunToolCall("Deploy"),), checkpoint="inspect")
        return sdk.DriverFinished("done")
    assert resume.checkpoint == "inspect"
    assert resume.results[0].is_error
    assert resume.results[0].structured["outcome_unknown"]
    return sdk.DriverFinished("Verify external state before retrying")
""",
    )
    service.install(package)
    await service.enable(PLUGIN)
    save = runtime.run_lifecycle.save_batch_state

    def interrupted(**kwargs):
        batch = kwargs["batch"]
        if batch is not None and any(call.result is not None for call in batch.calls):
            raise RuntimeError("interrupted before durable result")
        return save(**kwargs)

    monkeypatch.setattr(runtime.run_lifecycle, "save_batch_state", interrupted)
    try:
        failed = await runtime.run("deploy", driver_id=DRIVER)
        assert failed.run.status == "failed", failed.run.error
        assert len(calls) == 1
        saved = store.load_run(failed.session.session_id, failed.run.run_id)
        assert saved.driver_checkpoint.state == "pending"
        assert saved.active_tool_batch.calls[0].status == "running"
        monkeypatch.setattr(runtime.run_lifecycle, "save_batch_state", save)
        result = await runtime.continue_run(session_id=failed.session.session_id)
        assert result.run.status == "completed", result.run.error
        assert result.final_text == "Verify external state before retrying"
        assert len(calls) == 1
        history = store.load_runtime_session(result.session.session_id)
        receipt = next(item for item in history.messages if item.tool_result is not None)
        assert receipt.tool_result.producer.driver_id == DRIVER
        assert receipt.tool_result.is_error
    finally:
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
@pytest.mark.parametrize("tamper", [None, "identity", "text"])
async def test_finishing_model_response_preserves_one_authentic_assistant_message(
    tmp_path, kind, tamper
):
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    runtime, plugins, service = host(tmp_path, store, [])
    package = plugin_source(tmp_path, kind)
    set_driver_body(
        package,
        f"""async def review(context, resume):
    response = await context.models.request(tools=())
    return sdk.DriverFinished(
        {"'forged'" if tamper == "text" else "response.text"},
        response_id={"'unknown'" if tamper == "identity" else "response.response_id"},
    )
""",
    )
    service.install(package)
    await service.enable(PLUGIN)
    try:
        result = await runtime.run("review", driver_id=DRIVER)
        assert result.run.status == ("completed" if tamper is None else "failed"), result.run.error
        assistants = [
            item
            for item in store.load_runtime_session(result.session.session_id).messages
            if item.role == "assistant"
        ]
        assert len(assistants) == 1
        assert assistants[0].text_content == "reviewed"
        assert assistants[0].model_response is not None
        assert result.run.model_round_count == 1
        if tamper is None:
            assert result.final_text == "reviewed"
    finally:
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("store_type", [LocalSessionStore, SQLiteSessionStore])
async def test_default_driver_continues_native_facts_after_checkpoint_consumption_interruption(
    tmp_path, monkeypatch, store_type
):
    store = store_type(
        tmp_path / "sessions.sqlite3" if store_type is SQLiteSessionStore else tmp_path / "sessions"
    )
    store.initialize()
    calls = []
    runtime, plugins, _ = host(tmp_path, store, calls)
    runtime.adapter.provider = FauxProvider(
        event_batches=[
            [
                ProviderStreamEvent.from_tool_call(
                    {"id": "deploy", "name": "Deploy", "arguments": {}}
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="finished"),
                ProviderStreamEvent(type="done"),
            ],
        ]
    )
    consume = runtime.run_lifecycle.consume_driver_checkpoint
    try:
        waiting = await runtime.run("deploy")
        assert waiting.run.status == "waiting_approval", waiting.run.error
        assert waiting.run.driver.is_default
        assert waiting.run.driver_checkpoint.payload == "turn"

        def interrupted(session_id, run_id):
            consume(session_id, run_id)
            raise RuntimeError("interrupted after native checkpoint consumption")

        monkeypatch.setattr(runtime.run_lifecycle, "consume_driver_checkpoint", interrupted)
        failed = await runtime.approve_tool(
            session_id=waiting.session.session_id,
            run_id=waiting.run.run_id,
            approval_id=waiting.run.pending_approval_id,
            approved=True,
        )
        assert failed.run.status == "failed", failed.run.error
        assert failed.run.driver_checkpoint.state == "consumed"
        monkeypatch.setattr(runtime.run_lifecycle, "consume_driver_checkpoint", consume)
        resumed = await runtime.continue_run(session_id=waiting.session.session_id)
        assert resumed.run.status == "completed", resumed.run.error
        assert resumed.final_text == "finished"
        assert resumed.run.model_round_count == 2
        assert calls == ["deploy"]
        assistants = [
            item
            for item in store.load_runtime_session(resumed.session.session_id).messages
            if item.role == "assistant"
        ]
        assert len(assistants) == 2
    finally:
        await runtime.close()
        await plugins.close()


@pytest.mark.parametrize("kind", ["python-inprocess", "worker"])
async def test_driver_abort_waits_for_native_tool_cancellation_receipts(tmp_path, kind):
    from ouro.core.state import SessionRecord

    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    started, finalized = asyncio.Event(), asyncio.Event()

    async def wait_tool(invocation, context):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    runtime, plugins, service = host(tmp_path, store, [], mode="auto", handler=wait_tool)
    service.install(plugin_source(tmp_path, kind))
    await service.enable(PLUGIN)
    session = SessionRecord(title="cancel Driver tool")
    store.create_session(session)
    running = asyncio.create_task(
        runtime.run("deploy", session_id=session.session_id, driver_id=DRIVER)
    )
    try:
        async with asyncio.timeout(5):
            await started.wait()
            active = store.load_session_record(session.session_id)
            await runtime.abort_run(session_id=session.session_id, run_id=active.active_run_id)
            result = await running
        assert result.run.status == "aborted"
        assert finalized.is_set()
        events = store.load_session(session.session_id).events
        completed = next(event for event in events if event.event_type == "tool_call_completed")
        assert completed.payload["status"] == "aborted"
        kinds = [event.event_type for event in events]
        assert kinds.index("tool_call_completed") < kinds.index("tool_batch_aborted")
        assert kinds.index("tool_batch_aborted") < kinds.index("run_aborted")
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        await runtime.close()
        await plugins.close()
