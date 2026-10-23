from __future__ import annotations

import asyncio
import json
import shutil

import pytest

from ouro.application.observers import ObserverDispatcher
from ouro.application.runtime.configuration import RuntimeConfigurationResolver
from ouro.application.runtime.environment import RuntimeEnvironment
from ouro.application.runtime.service import RuntimeService
from ouro.config.queued_inputs import QueuedInputConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.cancellation import AbortSignal
from ouro.core.errors import PromptRejectedError
from ouro.core.observers import ContextPreparedObservation
from ouro.core.provider import ModelMetadata, ProviderStreamEvent, ToolCallDelta
from ouro.core.tool_policies import (
    ToolPolicyDecision,
    ToolPolicyRegistration,
    ToolPolicyRegistry,
)
from ouro.core.tools import ToolInvocation
from ouro.local_application import LocalApplication
from ouro.plugins.inprocess.contributions import adapt_sdk_contributions
from ouro.providers import FauxProvider
from ouro.sdk.plugins import v5 as sdk
from ouro.sessions import LocalSessionStore
from ouro.tools import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
    builtin_tools,
)
from ouro.tools.builtin.shell import detect_command_shell


@pytest.mark.asyncio
async def test_sdk_tool_receives_host_owned_workspace_and_cancellation_context(tmp_path) -> None:
    seen: dict[str, object] = {}

    async def inspect_workspace(call: sdk.ToolCall, context: sdk.ToolExecutionContext):
        seen.update(
            {
                "call_workspace": call.workspace,
                "context_workspace": context.workspace,
                "workspace_only": context.workspace_only,
                "resolved": context.resolve_path("nested/file.txt"),
                "aborted": context.is_aborted,
            }
        )
        await context.sleep(0)
        return sdk.ToolOutput(content="inspected")

    contributions = adapt_sdk_contributions(
        owner_id="example.plugin",
        contributions=sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="inspect",
                    exposed_name="inspect_workspace",
                    description="Inspect the workspace context.",
                    handler=inspect_workspace,
                )
            ]
        ),
    )
    signal = AbortSignal()
    result = await contributions.tools[0].execute(
        ToolInvocation(
            session_id="ses_1",
            run_id="run_1",
            tool_name="inspect_workspace",
        ),
        context=ToolExecutionContext(
            signal=signal,
            workspace=tmp_path,
            workspace_only=True,
        ),
    )

    assert result.content == "inspected"
    assert seen == {
        "call_workspace": str(tmp_path),
        "context_workspace": str(tmp_path),
        "workspace_only": True,
        "resolved": str(tmp_path / "nested" / "file.txt"),
        "aborted": False,
    }


@pytest.mark.asyncio
async def test_inprocess_plugin_tool_policy_can_restrict_builtin_tool(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = LocalSessionStore(home)
    store.initialize()
    tools = ToolRegistry()
    for tool in builtin_tools():
        tools.register(tool)
    shell = detect_command_shell()
    assert shell is not None
    provider = FauxProvider(
        event_batches=[
            [
                ProviderStreamEvent(
                    type="tool_call_delta",
                    tool_call_delta=ToolCallDelta(
                        index=0,
                        id_delta="call_guarded",
                        name_delta=shell.tool_name,
                        arguments_delta=json.dumps(
                            {"command": "echo unsafe > should-not-exist.txt"}
                        ),
                    ),
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="guard observed"),
                ProviderStreamEvent(type="done"),
            ],
        ]
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="guard", provider_id="faux"),
        ),
        tools=tools,
        default_workspace=workspace,
        environment=RuntimeEnvironment(home=home),
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    source = tmp_path / "guard-plugin"
    _write_guard_plugin(source, shell.tool_name)
    assert application.plugins is not None
    application.plugins.install(source)
    await application.start()
    assert (await application.plugins.enable("dev.ouro.guard")).status == "active"

    result = await runtime.run("run the guarded command")

    snapshot = store.load_runtime_session(result.session.session_id)
    tool_message = next(message for message in snapshot.messages if message.role == "tool_result")
    assert tool_message.tool_result is not None
    assert tool_message.tool_result.is_error is True
    assert "guard policy blocked the command" in tool_message.text_content
    assert not (workspace / "should-not-exist.txt").exists()
    persisted = store.load_session(result.session.session_id)
    policy_event = next(
        event for event in persisted.events if event.event_type == "tool_policy_evaluated"
    )
    assert policy_event.payload["mode"] == "deny"
    assert policy_event.payload["policy_ids"] == ["dev.ouro.guard/tool_policies/safe_shell"]

    await application.close()


@pytest.mark.asyncio
async def test_runtime_available_tools_follow_the_published_plugin_catalog(tmp_path) -> None:
    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    tools = ToolRegistry()
    for tool in builtin_tools():
        tools.register(tool)
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id="catalog", provider_id="faux"),
        ),
        tools=tools,
        environment=RuntimeEnvironment(home=home),
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    source = tmp_path / "catalog-plugin"
    _write_catalog_plugin(source)
    assert application.plugins is not None
    application.plugins.install(source)
    await application.start()

    try:
        assert "catalog__generation" not in {
            definition.name for definition in runtime.available_tool_definitions()
        }
        assert (await application.plugins.enable("dev.ouro.catalog")).status == "active"

        available = {
            definition.name: definition for definition in runtime.available_tool_definitions()
        }
        assert available["catalog__generation"].metadata["plugin_id"] == "dev.ouro.catalog"
        assert "catalog__generation" not in runtime.tools.names()

        assert (await application.plugins.disable("dev.ouro.catalog")).status == "disabled"
        assert "catalog__generation" not in {
            definition.name for definition in runtime.available_tool_definitions()
        }
    finally:
        await application.close()


@pytest.mark.asyncio
async def test_inprocess_prompt_policy_uses_the_run_catalog_and_denies_without_state(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id="prompt-policy", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment(home=home),
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    source = tmp_path / "prompt-policy-plugin"
    _write_prompt_policy_plugin(source)
    assert application.plugins is not None
    application.plugins.install(source)
    await application.start()
    assert (await application.plugins.enable("dev.ouro.prompt_guard")).status == "active"

    allowed = await runtime.run("ordinary input")
    seen = json.loads(
        (await application.plugins.execute_command("dev.ouro.prompt_guard:inspect")).output
    )
    generation = allowed.run.metadata["extension_generations"][0]
    assert seen["catalog_id"] == allowed.run.metadata["extension_catalog_id"]
    assert seen["execution_contract"] == generation["execution_contract_hash"]

    before = store.load_runtime_session(allowed.session.session_id)
    with pytest.raises(PromptRejectedError, match="prompt rejected by test guard"):
        await runtime.run("contains blocked marker", session_id=allowed.session.session_id)
    after = store.load_runtime_session(allowed.session.session_id)
    assert after.messages == before.messages
    assert after.runs == before.runs
    assert "contains blocked marker" not in after.session.model_dump_json()

    assert (await application.plugins.disable("dev.ouro.prompt_guard")).status == "disabled"
    after_disable = await runtime.run(
        "contains blocked marker after disable",
        session_id=allowed.session.session_id,
    )
    assert after_disable.run.status == "completed"

    await application.close()


@pytest.mark.asyncio
async def test_observer_timeout_releases_catalog_lease_after_plugin_disable(tmp_path) -> None:
    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id="observer-retire", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment(home=home),
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    source = tmp_path / "observer-retire-plugin"
    _write_observer_retire_plugin(source)
    assert application.plugins is not None
    assert application.plugin_runtime is not None
    application.plugins.install(source)
    await application.start()
    assert (await application.plugins.enable("dev.ouro.observer_retire")).status == "active"

    lease = application.plugin_runtime.snapshot()
    dispatcher = ObserverDispatcher(shutdown_timeout_seconds=1)
    dispatcher.enqueue(
        lease.observers,
        ContextPreparedObservation(
            session_id="ses_retire",
            run_id="run_retire",
            message_count=1,
            tool_count=0,
            model_round=0,
        ),
        release=lease.close,
    )
    for _ in range(100):
        observed = await application.plugins.execute_command("dev.ouro.observer_retire:inspect")
        if observed.output == "started":
            break
        await asyncio.sleep(0.005)
    assert observed.output == "started"

    assert (await application.plugins.disable("dev.ouro.observer_retire")).status == "disabled"
    stopped = home / "plugins" / "data" / "dev.ouro.observer_retire" / "stopped"
    for _ in range(100):
        if stopped.exists():
            break
        await asyncio.sleep(0.005)

    assert stopped.exists()
    assert stopped.read_text(encoding="utf-8") == "disable"
    await dispatcher.close()
    await application.close()


@pytest.mark.asyncio
async def test_model_decision_uses_current_catalog_after_prompt_admission(tmp_path) -> None:
    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(type="text_delta", text="allowed"),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(model_id="prompt-policy-swap", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment(home=home),
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    source = tmp_path / "prompt-policy-plugin"
    _write_prompt_policy_plugin(source)
    assert application.plugins is not None
    assert application.plugin_runtime is not None
    application.plugins.install(source)
    await application.start()
    assert (await application.plugins.enable("dev.ouro.prompt_guard")).status == "active"
    with application.plugin_runtime.snapshot() as initial:
        old_generation = initial.generations[0].generation_id

    run_task = asyncio.create_task(runtime.run("slow marker input"))
    for _ in range(100):
        seen = await application.plugins.execute_command("dev.ouro.prompt_guard:inspect")
        if json.loads(seen.output) is not None:
            break
        await asyncio.sleep(0.005)
    assert json.loads(seen.output) is not None
    assert store.list_sessions() == []

    assert await application.plugin_runtime.restart("dev.ouro.prompt_guard") == "active"
    with application.plugin_runtime.snapshot() as current:
        assert current.generations[0].generation_id != old_generation

    result = await run_task
    assert result.run.status == "completed"
    assert result.run.metadata["extension_generations"][0]["generation_id"] != old_generation

    await application.close()


@pytest.mark.asyncio
async def test_queued_inputs_share_current_consumption_environment(tmp_path) -> None:
    class GateProvider:
        provider_id = "gate"

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def stream(
            self, *, model, system_prompt="", messages, tools=None, request_context=None
        ):
            del model, system_prompt, messages, tools
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                await self.release.wait()
            yield ProviderStreamEvent(type="text_delta", text="done")
            yield ProviderStreamEvent(type="done")

        async def check_connection(self, model, *, request_context=None):
            raise AssertionError(f"unexpected connection check: {model.model_id}")

    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    provider = GateProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="prompt-policy", provider_id="gate"),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment(home=home),
        queued_input_config=QueuedInputConfig(consumption_mode="all_at_once"),
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    source = tmp_path / "prompt-policy-plugin"
    _write_prompt_policy_plugin(source)
    assert application.plugins is not None
    assert application.plugin_runtime is not None
    application.plugins.install(source)
    await application.start()
    assert (await application.plugins.enable("dev.ouro.prompt_guard")).status == "active"

    first_task = asyncio.create_task(runtime.run("first input"))
    await asyncio.wait_for(provider.started.wait(), 5)
    session = store.list_sessions()[0]
    first_run = store.load_runtime_session(session.session_id).runs[0]
    old_generation = first_run.metadata["extension_generations"][0]["generation_id"]

    await runtime.run("queued input", session_id=session.session_id)
    queued = store.load_session_record(session.session_id).metadata["follow_up_queue"][0]
    assert queued["prompt_policy_checked"] is True
    assert queued["prospective_run_id"]
    assert queued["extension_generations"][0]["generation_id"] == old_generation

    assert await application.plugin_runtime.restart("dev.ouro.prompt_guard") == "active"
    with application.plugin_runtime.snapshot() as current:
        assert current.generations[0].generation_id != old_generation
        current_generation = current.generations[0].generation_id

    await runtime.run("queued under current catalog", session_id=session.session_id)
    queued_items = store.load_session_record(session.session_id).metadata["follow_up_queue"]
    assert len(queued_items) == 2
    assert queued_items[1]["extension_generations"][0]["generation_id"] == current_generation

    provider.release.set()
    await first_task
    completed = store.load_runtime_session(session.session_id)
    assert len(completed.runs) == 2
    queued_run = completed.runs[1]
    assert queued_run.run_id == queued["prospective_run_id"]
    assert queued_run.metadata["extension_generations"][0]["generation_id"] == current_generation
    user_texts = [message.text_content for message in completed.messages if message.role == "user"]
    assert user_texts == ["first input", "queued input", "queued under current catalog"]

    await application.close()


@pytest.mark.asyncio
async def test_steering_admission_and_consumption_use_current_policy(tmp_path) -> None:
    class GateProvider:
        provider_id = "gate"

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def stream(
            self, *, model, system_prompt="", messages, tools=None, request_context=None
        ):
            del model, system_prompt, messages, tools
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                await self.release.wait()
            yield ProviderStreamEvent(type="text_delta", text="done")
            yield ProviderStreamEvent(type="done")

        async def check_connection(self, model, *, request_context=None):
            raise AssertionError(f"unexpected connection check: {model.model_id}")

    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    provider = GateProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="prompt-policy-steer", provider_id="gate"),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment(home=home),
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    source = tmp_path / "prompt-policy-plugin"
    _write_prompt_policy_plugin(source)
    assert application.plugins is not None
    assert application.plugin_runtime is not None
    application.plugins.install(source)
    await application.start()
    assert (await application.plugins.enable("dev.ouro.prompt_guard")).status == "active"

    first_task = asyncio.create_task(runtime.run("first input"))
    await asyncio.wait_for(provider.started.wait(), 5)
    session = store.list_sessions()[0]
    active_run = store.load_runtime_session(session.session_id).runs[0]
    active_catalog_id = active_run.metadata["extension_catalog_id"]
    active_generation = active_run.metadata["extension_generations"][0]["generation_id"]

    assert await application.plugin_runtime.restart("dev.ouro.prompt_guard") == "active"
    await runtime.run(
        "steer under active catalog",
        session_id=session.session_id,
        queue_mode="steer",
    )
    queued = store.load_session_record(session.session_id).metadata["follow_up_queue"][0]
    assert queued["extension_catalog_id"] != active_catalog_id
    assert queued["extension_generations"][0]["generation_id"] != active_generation
    seen = json.loads(
        (await application.plugins.execute_command("dev.ouro.prompt_guard:inspect")).output
    )
    assert seen["catalog_id"] == queued["extension_catalog_id"]

    provider.release.set()
    await first_task
    completed = store.load_runtime_session(session.session_id)
    assert len(completed.runs) == 1
    assert "follow_up_queue" not in completed.session.metadata
    assert provider.calls == 2

    await application.close()


@pytest.mark.asyncio
async def test_tool_controlled_tool_bypasses_host_permission_but_not_tool_policy(tmp_path) -> None:
    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    tools = ToolRegistry()

    async def control_harness(invocation, context) -> ToolResult:
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="harness controlled",
        )

    tools.register(
        Tool(
            definition=ToolDefinition(
                name="ControlHarness",
                description="Control the harness.",
                permission_mode="auto",
                permission_control="tool",
            ),
            handler=control_harness,
        )
    )
    evaluated: list[str] = []

    def reject(call):
        evaluated.append(call.tool_name)
        return ToolPolicyDecision(action="deny", reason="host policy denied")

    policies = ToolPolicyRegistry()
    policies.replace_owner(
        "guard",
        (
            ToolPolicyRegistration(
                plugin_id="dev.ouro.guard",
                policy_id="deny_all",
                handler=reject,
            ),
        ),
    )
    provider = FauxProvider(
        event_batches=[
            [
                ProviderStreamEvent.from_tool_call(
                    {
                        "id": "call_control",
                        "name": "ControlHarness",
                        "arguments": {},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="controlled"),
                ProviderStreamEvent(type="done"),
            ],
        ]
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="control", provider_id="faux"),
        ),
        tools=tools,
        tool_policies=policies,
        permission_overrides={"ControlHarness": "deny"},
        environment=RuntimeEnvironment(home=home),
    )

    result = await runtime.run("control the harness", approval_mode="deny")

    assert result.run.status == "completed"
    assert result.tool_results[0].structured["error_type"] == "ToolPermissionDenied"
    assert result.tool_results[0].is_error is True
    assert evaluated == ["ControlHarness"]
    events = store.load_session(result.session.session_id).events
    assert any(event.event_type == "tool_policy_evaluated" for event in events)
    await runtime.close()


@pytest.mark.asyncio
async def test_tool_policy_is_rechecked_at_the_execution_boundary(tmp_path) -> None:
    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    tools = ToolRegistry()
    executed: list[str] = []

    async def guarded_tool(invocation, context) -> ToolResult:
        executed.append(invocation.tool_call_id)
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="executed",
        )

    tools.register(
        Tool(
            definition=ToolDefinition(
                name="GuardedTool",
                description="Exercise a changing policy.",
            ),
            handler=guarded_tool,
        )
    )
    evaluations = 0

    def tighten_after_preflight(_call):
        nonlocal evaluations
        evaluations += 1
        if evaluations == 1:
            return ToolPolicyDecision()
        return ToolPolicyDecision(action="deny", reason="permission was revoked")

    policies = ToolPolicyRegistry()
    policies.replace_owner(
        "guard",
        (
            ToolPolicyRegistration(
                plugin_id="dev.ouro.guard",
                policy_id="runtime_revocation",
                handler=tighten_after_preflight,
            ),
        ),
    )
    provider = FauxProvider(
        event_batches=[
            [
                ProviderStreamEvent.from_tool_call(
                    {
                        "id": "call_guarded",
                        "name": "GuardedTool",
                        "arguments": {},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="revocation observed"),
                ProviderStreamEvent(type="done"),
            ],
        ]
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="guard", provider_id="faux"),
        ),
        tools=tools,
        tool_policies=policies,
        environment=RuntimeEnvironment(home=home),
    )

    result = await runtime.run("exercise revocation")

    assert result.run.status == "completed"
    assert evaluations == 2
    assert executed == []
    assert result.tool_results[0].structured["error_type"] == "ToolPermissionDenied"
    policy_event = next(
        event
        for event in store.load_session(result.session.session_id).events
        if event.event_type == "tool_policy_evaluated"
    )
    assert policy_event.payload["phase"] == "execution_boundary"
    await runtime.close()


@pytest.mark.asyncio
async def test_execution_boundary_can_park_for_a_new_durable_approval(tmp_path) -> None:
    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    tools = ToolRegistry()
    executed: list[str] = []

    async def guarded_tool(invocation, context) -> ToolResult:
        executed.append(invocation.tool_call_id)
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="executed after approval",
        )

    tools.register(
        Tool(
            definition=ToolDefinition(
                name="ApprovalBoundaryTool",
                description="Require approval only at the execution boundary.",
            ),
            handler=guarded_tool,
        )
    )
    evaluations = 0

    def tighten_after_preflight(_call):
        nonlocal evaluations
        evaluations += 1
        if evaluations == 1:
            return ToolPolicyDecision()
        return ToolPolicyDecision(action="require_approval", reason="review now required")

    policies = ToolPolicyRegistry()
    policies.replace_owner(
        "guard",
        (
            ToolPolicyRegistration(
                plugin_id="dev.ouro.guard",
                policy_id="runtime_approval",
                handler=tighten_after_preflight,
            ),
        ),
    )
    provider = FauxProvider(
        event_batches=[
            [
                ProviderStreamEvent.from_tool_call(
                    {
                        "id": "call_approval_boundary",
                        "name": "ApprovalBoundaryTool",
                        "arguments": {},
                    },
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="approval observed"),
                ProviderStreamEvent(type="done"),
            ],
        ]
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="guard", provider_id="faux"),
        ),
        tools=tools,
        tool_policies=policies,
        environment=RuntimeEnvironment(home=home),
    )

    waiting = await runtime.run("exercise execution-boundary approval")

    assert waiting.run.status == "waiting_approval"
    assert waiting.run.pending_approval_id is not None
    assert executed == []
    completed = await runtime.approve_tool(
        session_id=waiting.session.session_id,
        run_id=waiting.run.run_id,
        approval_id=waiting.run.pending_approval_id,
        approved=True,
    )

    assert completed.run.status == "completed"
    assert evaluations == 3
    assert executed == ["call_approval_boundary"]
    await runtime.close()


@pytest.mark.asyncio
async def test_waiting_calls_resume_on_current_matching_implementation(tmp_path) -> None:
    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    tools = ToolRegistry()
    for tool in builtin_tools():
        tools.register(tool)
    provider = FauxProvider(
        event_batches=[
            _tool_call_batch("call_old", "catalog__generation"),
            _tool_call_batch("call_new", "catalog__generation"),
            [
                ProviderStreamEvent(type="text_delta", text="old complete"),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="new complete"),
                ProviderStreamEvent(type="done"),
            ],
        ]
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="catalog", provider_id="faux"),
        ),
        tools=tools,
        environment=RuntimeEnvironment(home=home),
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    source = tmp_path / "catalog-plugin"
    _write_catalog_plugin(source)
    assert application.plugins is not None
    assert application.plugin_runtime is not None
    application.plugins.install(source)
    await application.start()
    assert (await application.plugins.enable("dev.ouro.catalog")).status == "active"

    old = await runtime.run("use the old catalog")
    assert old.run.status == "waiting_approval"
    old_generation = old.run.metadata["extension_generations"][0]["generation_id"]
    old_catalog_id = old.run.metadata["extension_catalog_id"]

    assert await application.plugin_runtime.restart("dev.ouro.catalog") == "active"
    old_command = await application.plugins.execute_command(
        "dev.ouro.catalog:generation",
        session_id=old.session.session_id,
    )
    current_command = await application.plugins.execute_command("dev.ouro.catalog:generation")
    new = await runtime.run("use the new catalog")
    assert new.run.status == "waiting_approval"
    new_generation = new.run.metadata["extension_generations"][0]["generation_id"]
    assert new_generation != old_generation
    assert new.run.metadata["extension_catalog_id"] != old_catalog_id
    assert old_command.output == new_generation
    assert current_command.output == new_generation

    old_completed = await runtime.approve_tool(
        session_id=old.session.session_id,
        run_id=old.run.run_id,
        approval_id=old.run.pending_approval_id,
        approved=True,
    )
    new_completed = await runtime.approve_tool(
        session_id=new.session.session_id,
        run_id=new.run.run_id,
        approval_id=new.run.pending_approval_id,
        approved=True,
    )
    assert old_completed.run.status == "completed"
    assert new_completed.run.status == "completed"
    old_tool_result = next(
        message
        for message in store.load_runtime_session(old.session.session_id).messages
        if message.role == "tool_result"
    )
    new_tool_result = next(
        message
        for message in store.load_runtime_session(new.session.session_id).messages
        if message.role == "tool_result"
    )
    assert old_tool_result.text_content == new_generation
    assert new_tool_result.text_content == new_generation

    await application.close()


@pytest.mark.asyncio
async def test_waiting_approval_rebuilds_same_execution_contract_after_restart(tmp_path) -> None:
    home = tmp_path / "home"
    source = tmp_path / "catalog-plugin"
    _write_catalog_plugin(source)

    first_store = LocalSessionStore(home)
    first_store.initialize()
    first_provider = FauxProvider(
        event_batches=[_tool_call_batch("call_rebuild", "catalog__generation")]
    )
    first_runtime = RuntimeService(
        store=first_store,
        adapter=RuntimeAdapter(
            first_provider,
            ModelMetadata(model_id="contract", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment(home=home),
    )
    first_application = LocalApplication.from_runtime(first_runtime, home=home)
    assert first_application.plugins is not None
    first_application.plugins.install(source)
    await first_application.start()
    assert (await first_application.plugins.enable("dev.ouro.catalog")).status == "active"

    waiting = await first_runtime.run("wait for approval")
    assert waiting.run.status == "waiting_approval"
    persisted_generation = waiting.run.metadata["extension_generations"][0]
    persisted_batch = waiting.run.active_tool_batch
    assert persisted_batch is not None
    persisted_call = persisted_batch.calls[0]
    assert persisted_call.invocation is not None
    assert persisted_call.approval is not None
    assert (
        persisted_call.invocation.execution_contract_hash
        == persisted_generation["execution_contract_hash"]
    )
    assert (
        persisted_call.approval.execution_contract_hash
        == persisted_generation["execution_contract_hash"]
    )
    await first_application.close()

    second_store = LocalSessionStore(home)
    second_store.initialize()
    second_provider = FauxProvider(
        event_batches=[
            [
                ProviderStreamEvent(type="text_delta", text="resumed"),
                ProviderStreamEvent(type="done"),
            ]
        ]
    )
    second_runtime = RuntimeService(
        store=second_store,
        adapter=RuntimeAdapter(
            second_provider,
            ModelMetadata(model_id="contract", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment(home=home),
    )
    second_application = LocalApplication.from_runtime(second_runtime, home=home)
    await second_application.start()
    assert second_application.plugin_runtime is not None
    with second_application.plugin_runtime.snapshot() as rebuilt:
        rebuilt_generation = rebuilt.generations[0]
    assert rebuilt_generation.generation_id != persisted_generation["generation_id"]
    assert (
        rebuilt_generation.execution_contract_hash
        == persisted_generation["execution_contract_hash"]
    )

    completed = await second_runtime.approve_tool(
        session_id=waiting.session.session_id,
        run_id=waiting.run.run_id,
        approval_id=waiting.run.pending_approval_id,
        approved=True,
    )
    assert completed.run.status == "completed"
    tool_result = next(
        message
        for message in second_store.load_runtime_session(waiting.session.session_id).messages
        if message.role == "tool_result"
    )
    assert tool_result.text_content == rebuilt_generation.generation_id
    await second_application.close()


@pytest.mark.asyncio
async def test_resumable_plugin_tool_rebuilds_original_execution_contract_after_restart(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    source = tmp_path / "resumable-plugin"
    _write_resumable_plugin(source)

    first_store = LocalSessionStore(home)
    first_store.initialize()
    first_runtime = RuntimeService(
        store=first_store,
        adapter=RuntimeAdapter(
            FauxProvider(event_batches=[_tool_call_batch("call_resume", "resume__workflow")]),
            ModelMetadata(model_id="resume", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment(home=home),
    )
    first_application = LocalApplication.from_runtime(first_runtime, home=home)
    assert first_application.plugins is not None
    first_application.plugins.install(source)
    await first_application.start()
    assert (await first_application.plugins.enable("dev.ouro.resume")).status == "active"

    waiting = await first_runtime.run("start the workflow")
    persisted_generation = waiting.run.metadata["extension_generations"][0]
    persisted_batch = waiting.run.active_tool_batch
    assert waiting.run.status == "waiting_user"
    assert persisted_batch is not None
    persisted_call = persisted_batch.calls[0]
    assert persisted_call.invocation is not None
    assert persisted_call.input_request is not None
    assert (
        persisted_call.input_request.data["generation_id"] == persisted_generation["generation_id"]
    )
    assert (
        persisted_call.invocation.execution_contract_hash
        == persisted_generation["execution_contract_hash"]
    )
    await first_application.close()

    second_store = LocalSessionStore(home)
    second_store.initialize()
    second_runtime = RuntimeService(
        store=second_store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent(type="text_delta", text="resumed"),
                        ProviderStreamEvent(type="done"),
                    ]
                ]
            ),
            ModelMetadata(model_id="resume", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment(home=home),
    )
    second_application = LocalApplication.from_runtime(second_runtime, home=home)
    await second_application.start()
    assert second_application.plugin_runtime is not None
    with second_application.plugin_runtime.snapshot() as rebuilt:
        rebuilt_generation = rebuilt.generations[0]
    assert rebuilt_generation.generation_id != persisted_generation["generation_id"]
    assert (
        rebuilt_generation.execution_contract_hash
        == persisted_generation["execution_contract_hash"]
    )

    completed = await second_runtime.answer_user(
        session_id=waiting.session.session_id,
        run_id=waiting.run.run_id,
        answer={"choice": "accept"},
    )

    assert completed.run.status == "completed"
    assert completed.tool_results[0].structured == {
        "answer": {"choice": "accept"},
        "generation_id": rebuilt_generation.generation_id,
    }
    await second_application.close()


@pytest.mark.asyncio
async def test_waiting_approval_rejects_changed_contract_after_restart(
    tmp_path,
) -> None:
    home = tmp_path / "home"
    first_source = tmp_path / "catalog-v1"
    second_source = tmp_path / "catalog-v2"
    _write_catalog_plugin(first_source, version="1.0.0")
    _write_catalog_plugin(second_source, version="2.0.0")

    first_store = LocalSessionStore(home)
    first_store.initialize()
    first_runtime = RuntimeService(
        store=first_store,
        adapter=RuntimeAdapter(
            FauxProvider(event_batches=[_tool_call_batch("call_stale", "catalog__generation")]),
            ModelMetadata(model_id="contract", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment(home=home),
    )
    first_application = LocalApplication.from_runtime(first_runtime, home=home)
    assert first_application.plugins is not None
    first = first_application.plugins.install(first_source)
    second = await first_application.plugins.update("dev.ouro.catalog", second_source)
    await first_application.start()
    assert (await first_application.plugins.enable("dev.ouro.catalog")).status == "active"
    waiting = await first_runtime.run("retain the first contract")
    assert waiting.run.status == "waiting_approval"
    await first_application.close()

    second_store = LocalSessionStore(home)
    second_store.initialize()
    second_runtime = RuntimeService(
        store=second_store,
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id="contract", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment(home=home),
    )
    second_application = LocalApplication.from_runtime(second_runtime, home=home)
    await second_application.start()
    assert second_application.plugins is not None
    assert (
        await second_application.plugins.select(
            "dev.ouro.catalog",
            second.package_digest,
        )
    ).status == "active"
    assert second_application.plugin_runtime is not None
    with second_application.plugin_runtime.snapshot() as current:
        selected_generation = current.generations[0]
    completed = await second_runtime.approve_tool(
        session_id=waiting.session.session_id,
        run_id=waiting.run.run_id,
        approval_id=waiting.run.pending_approval_id,
        approved=True,
    )
    assert completed.run.status == "completed"
    tool_result = next(
        message
        for message in second_store.load_runtime_session(waiting.session.session_id).messages
        if message.role == "tool_result"
    )
    assert tool_result.tool_result.is_error
    assert tool_result.text_content != selected_generation.generation_id
    assert (
        await second_application.plugins.remove(
            "dev.ouro.catalog",
            package_digest=first.package_digest,
        )
    ).package_digest == first.package_digest
    await second_application.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_waiting_approval_closes_missing_tool_and_continues(
    tmp_path,
    approved: bool,
) -> None:
    home = tmp_path / "home"
    source = tmp_path / "catalog-plugin"
    _write_catalog_plugin(source)
    first_store = LocalSessionStore(home)
    first_store.initialize()
    first_runtime = RuntimeService(
        store=first_store,
        adapter=RuntimeAdapter(
            FauxProvider(event_batches=[_tool_call_batch("call_missing", "catalog__generation")]),
            ModelMetadata(model_id="contract", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment(home=home),
    )
    first_application = LocalApplication.from_runtime(first_runtime, home=home)
    assert first_application.plugins is not None
    installed = first_application.plugins.install(source)
    await first_application.start()
    assert (await first_application.plugins.enable("dev.ouro.catalog")).status == "active"
    waiting = await first_runtime.run("retain package bytes")
    assert waiting.run.status == "waiting_approval"
    await first_application.close()
    shutil.rmtree(home / "plugins" / "store" / "dev.ouro.catalog" / installed.package_digest)

    second_store = LocalSessionStore(home)
    second_store.initialize()
    second_runtime = RuntimeService(
        store=second_store,
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id="contract", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        environment=RuntimeEnvironment(home=home),
    )
    second_application = LocalApplication.from_runtime(second_runtime, home=home)
    await second_application.start()

    result = await second_runtime.approve_tool(
        session_id=waiting.session.session_id,
        run_id=waiting.run.run_id,
        approval_id=waiting.run.pending_approval_id,
        approved=approved,
        reason="not allowed",
    )
    persisted = second_store.load_session(waiting.session.session_id)
    approval_resolved = next(
        event for event in persisted.events if event.event_type == "approval_resolved"
    )

    assert result.run.status == "completed"
    assert result.run.pending_approval_id is None
    tool_results = [message.tool_result for message in persisted.messages if message.tool_result]
    assert len(tool_results) == 1
    assert tool_results[0].is_error
    assert approval_resolved.payload["approved"] is approved
    assert approval_resolved.payload["tool_call_status"] == ("ready" if approved else "rejected")
    removed = await second_application.plugins.remove(
        "dev.ouro.catalog",
        package_digest=installed.package_digest,
    )
    assert removed.package_digest == installed.package_digest
    await second_application.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_waiting_approval_survives_unrelated_builtin_change(
    tmp_path,
    approved: bool,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first_store = LocalSessionStore(home)
    first_store.initialize()
    first_tools = ToolRegistry()
    for tool in builtin_tools():
        first_tools.register(tool)
    first_runtime = RuntimeService(
        store=first_store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent.from_tool_call(
                            {
                                "id": "call_stale_builtin",
                                "name": "Write",
                                "arguments": {
                                    "path": "note.txt",
                                    "content": "content",
                                },
                            }
                        ),
                        ProviderStreamEvent(type="done"),
                    ]
                ]
            ),
            ModelMetadata(model_id="contract", provider_id="faux"),
        ),
        tools=first_tools,
        default_workspace=workspace,
        environment=RuntimeEnvironment(home=home),
    )
    first_application = LocalApplication.from_runtime(first_runtime, home=home)
    await first_application.start()
    waiting = await first_runtime.run("write a file")
    assert waiting.run.status == "waiting_approval"

    generations = [dict(item) for item in waiting.run.metadata["extension_generations"]]
    builtin_generation = next(item for item in generations if item["plugin_class"] == "builtin")
    stale_contract_hash = "f" * 64
    builtin_generation["release_id"] = stale_contract_hash
    builtin_generation["builtin_contract_hash"] = stale_contract_hash
    metadata = dict(waiting.run.metadata)
    metadata["extension_generations"] = generations
    first_store.save_run(waiting.run.model_copy(update={"metadata": metadata}))
    await first_application.close()

    second_store = LocalSessionStore(home)
    second_store.initialize()
    second_tools = ToolRegistry()
    for tool in builtin_tools():
        second_tools.register(tool)
    second_runtime = RuntimeService(
        store=second_store,
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id="contract", provider_id="faux"),
        ),
        tools=second_tools,
        default_workspace=workspace,
        environment=RuntimeEnvironment(home=home),
    )
    second_application = LocalApplication.from_runtime(second_runtime, home=home)
    await second_application.start()

    result = await second_runtime.approve_tool(
        session_id=waiting.session.session_id,
        run_id=waiting.run.run_id,
        approval_id=waiting.run.pending_approval_id,
        approved=approved,
        reason="not allowed",
    )
    persisted = second_store.load_session(waiting.session.session_id)
    approval_resolved = next(
        event for event in persisted.events if event.event_type == "approval_resolved"
    )

    assert result.run.status == "completed"
    assert result.run.pending_approval_id is None
    tool_results = [message.tool_result for message in persisted.messages if message.tool_result]
    assert len(tool_results) == 1
    assert tool_results[0].is_error is (not approved)
    assert approval_resolved.payload["approved"] is approved
    assert approval_resolved.payload["tool_call_status"] == ("ready" if approved else "rejected")
    await second_application.close()


@pytest.mark.asyncio
async def test_plugin_skill_prompt_is_resolved_from_each_run_catalog(tmp_path) -> None:
    class CapturingProvider(FauxProvider):
        def __init__(self) -> None:
            super().__init__()
            self.system_prompts: list[str] = []

        async def stream(
            self, *, model, system_prompt="", messages, tools=None, request_context=None
        ):
            self.system_prompts.append(system_prompt)
            async for event in super().stream(
                model=model,
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
            ):
                yield event

    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    provider = CapturingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="skills", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        skill_catalog_resolver=RuntimeConfigurationResolver(home).skills,
        environment=RuntimeEnvironment(home=home),
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    source = tmp_path / "skill-plugin"
    _write_skill_plugin(source)
    assert application.plugins is not None
    application.plugins.install(source)
    await application.start()
    assert (await application.plugins.enable("dev.ouro.skill_prompt")).status == "active"

    enabled_run = await runtime.run("use the calendar skill")
    assert enabled_run.run.status == "completed"
    assert "<name>calendar</name>" in provider.system_prompts[-1]
    assert "Manage the personal calendar." in provider.system_prompts[-1]

    local_skill = home / "skills" / "writer" / "SKILL.md"
    local_skill.parent.mkdir(parents=True)
    local_skill.write_text(
        "---\nname: writer\ndescription: Write project notes.\n---\nWrite notes.\n",
        encoding="utf-8",
    )
    await runtime.run("Write project notes", session_id=enabled_run.session.session_id)
    assert "<name>writer</name>" in provider.system_prompts[-1]
    assert "<name>calendar</name>" in provider.system_prompts[-1]

    assert (await application.plugins.disable("dev.ouro.skill_prompt")).status == "disabled"
    disabled_run = await runtime.run("answer without the plugin")
    assert disabled_run.run.status == "completed"
    assert "<name>calendar</name>" not in provider.system_prompts[-1]

    await application.close()


@pytest.mark.asyncio
async def test_tool_policy_rechecks_user_modified_approval_arguments(tmp_path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = LocalSessionStore(home)
    store.initialize()
    tools = ToolRegistry()
    for tool in builtin_tools():
        tools.register(tool)
    policies = ToolPolicyRegistry()
    policies.replace_owner(
        "guard",
        (
            ToolPolicyRegistration(
                plugin_id="dev.ouro.guard",
                policy_id="protected_path",
                handler=lambda call: (
                    ToolPolicyDecision(
                        action="deny",
                        reason="protected path cannot be written",
                    )
                    if call.arguments.get("path") == "protected.txt"
                    else ToolPolicyDecision()
                ),
            ),
        ),
    )
    provider = FauxProvider(
        event_batches=[
            [
                ProviderStreamEvent(
                    type="tool_call_delta",
                    tool_call_delta=ToolCallDelta(
                        index=0,
                        id_delta="call_write",
                        name_delta="Write",
                        arguments_delta=json.dumps({"path": "safe.txt", "content": "safe"}),
                    ),
                ),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="write was blocked"),
                ProviderStreamEvent(type="done"),
            ],
        ]
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="guard", provider_id="faux"),
        ),
        tools=tools,
        tool_policies=policies,
        default_workspace=workspace,
        environment=RuntimeEnvironment(home=home),
    )

    waiting = await runtime.run("write a file")
    assert waiting.run.status == "waiting_approval"
    assert waiting.run.pending_approval_id is not None

    completed = await runtime.approve_tool(
        session_id=waiting.session.session_id,
        run_id=waiting.run.run_id,
        approval_id=waiting.run.pending_approval_id,
        approved=True,
        arguments_override={"path": "protected.txt", "content": "blocked"},
    )

    assert completed.run.status == "completed"
    assert not (workspace / "safe.txt").exists()
    assert not (workspace / "protected.txt").exists()
    snapshot = store.load_runtime_session(waiting.session.session_id)
    tool_message = next(message for message in snapshot.messages if message.role == "tool_result")
    assert "protected path cannot be written" in tool_message.text_content
    events = store.load_session(waiting.session.session_id).events
    reevaluation = next(
        event
        for event in events
        if event.event_type == "tool_policy_evaluated"
        and event.payload.get("phase") == "execution_boundary"
    )
    assert reevaluation.payload["mode"] == "deny"


def _write_guard_plugin(source, command_tool_name: str) -> None:
    manifest_dir = source / ".ouro-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "plugin_id": "dev.ouro.guard",
                "name": "Guard",
                "version": "1.0.0",
                "namespace": "guard",
                "engines": {"ouro": ">=0.3.0,<0.4.0", "plugin_api": "^5.0.0"},
                "runtime": {
                    "kind": "python-inprocess",
                    "entrypoint": "guard_plugin:create_plugin",
                },
                "contributes": {
                    "tool_policies": [{"id": "safe_shell", "priority": 10, "timeout_seconds": 2}]
                },
                "permissions": [],
                "dependencies": {"plugins": []},
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    plugin_source = """from ouro.sdk.plugins.v5 import (
    ContributionSet,
    ToolPolicyContribution,
    ToolPolicyDecision,
)

class Plugin:
    async def prepare(self):
        async def safe_shell(call):
            command = str(call.arguments.get('command', ''))
            if call.tool_name == '__COMMAND_TOOL__' and 'unsafe' in command:
                return ToolPolicyDecision(
                    action='deny',
                    reason='guard policy blocked the command',
                )
            return ToolPolicyDecision()

        return ContributionSet(
            tool_policies=[ToolPolicyContribution(id='safe_shell', handler=safe_shell)]
        )

    async def start(self): pass
    async def stop(self, reason, deadline): pass

def create_plugin(context):
    return Plugin()
""".replace("__COMMAND_TOOL__", command_tool_name)
    (source / "guard_plugin.py").write_text(
        plugin_source,
        encoding="utf-8",
    )


def _write_observer_retire_plugin(source) -> None:
    manifest_dir = source / ".ouro-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "plugin_id": "dev.ouro.observer_retire",
                "name": "Observer Retire",
                "version": "1.0.0",
                "namespace": "observer_retire",
                "engines": {"ouro": ">=0.3.0,<0.4.0", "plugin_api": "^5.0.0"},
                "runtime": {
                    "kind": "python-inprocess",
                    "entrypoint": "observer_retire_plugin:create_plugin",
                },
                "contributes": {
                    "commands": [{"id": "inspect", "description": "Read observer state."}],
                    "observers": [
                        {
                            "id": "stuck",
                            "event": "context_prepared",
                            "timeout_seconds": 0.2,
                        }
                    ],
                },
                "permissions": [],
                "dependencies": {"plugins": []},
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    (source / "observer_retire_plugin.py").write_text(
        """import asyncio
from ouro.sdk.plugins.v5 import CommandContribution, ContributionSet, ObserverContribution

class Plugin:
    def __init__(self, context):
        self.context = context
        self.started = False

    async def prepare(self):
        async def observe(_observation):
            self.started = True
            await asyncio.Event().wait()

        async def inspect(_call):
            return 'started' if self.started else 'idle'

        return ContributionSet(
            observers=[ObserverContribution(
                id='stuck', event='context_prepared', handler=observe,
            )],
            commands=[CommandContribution(
                id='inspect', description='Read observer state.', handler=inspect,
            )],
        )

    async def start(self): pass

    async def stop(self, reason, deadline):
        self.context.data.write_bytes('stopped', reason.encode('utf-8'))

def create_plugin(context):
    return Plugin(context)
""",
        encoding="utf-8",
    )


def _write_prompt_policy_plugin(source) -> None:
    manifest_dir = source / ".ouro-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "plugin_id": "dev.ouro.prompt_guard",
                "name": "Prompt Guard",
                "version": "1.0.0",
                "namespace": "prompt_guard",
                "engines": {"ouro": ">=0.3.0,<0.4.0", "plugin_api": "^5.0.0"},
                "runtime": {
                    "kind": "python-inprocess",
                    "entrypoint": "prompt_guard_plugin:create_plugin",
                },
                "contributes": {
                    "commands": [{"id": "inspect", "description": "Read policy observation."}],
                    "prompt_policies": [
                        {"id": "blocked_marker", "priority": 10, "timeout_seconds": 2}
                    ],
                },
                "permissions": [
                    {
                        "id": "sessions.prompt.inspect",
                        "reason": "Reject a test marker before prompt persistence.",
                    }
                ],
                "dependencies": {"plugins": []},
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    (source / "prompt_guard_plugin.py").write_text(
        """import asyncio
import json
from ouro.sdk.plugins.v5 import (
    CommandContribution,
    ContributionSet,
    PromptPolicyContribution,
    PromptPolicyDecision,
)

class Plugin:
    def __init__(self, context):
        self.context = context
        self.plugin_id = context.identity.plugin_id
        self.seen = None

    async def prepare(self):
        async def inspect_prompt(call):
            self.seen = {
                'catalog_id': call.catalog_id,
                'execution_contract': call.execution_contracts.get(self.plugin_id),
            }
            if 'slow marker' in call.prompt:
                await asyncio.sleep(0.1)
            if 'blocked marker' in call.prompt:
                return PromptPolicyDecision(
                    action='deny', reason='prompt rejected by test guard'
                )
            return PromptPolicyDecision()

        async def inspect(_call):
            return json.dumps(self.seen)

        return ContributionSet(
            prompt_policies=[PromptPolicyContribution(
                id='blocked_marker', handler=inspect_prompt,
            )],
            commands=[CommandContribution(
                id='inspect', description='Read policy observation.', handler=inspect,
            )],
        )

    async def start(self): pass
    async def stop(self, reason, deadline): pass

def create_plugin(context):
    return Plugin(context)
""",
        encoding="utf-8",
    )


def _tool_call_batch(call_id: str, tool_name: str) -> list[ProviderStreamEvent]:
    return [
        ProviderStreamEvent(
            type="tool_call_delta",
            tool_call_delta=ToolCallDelta(
                index=0,
                id_delta=call_id,
                name_delta=tool_name,
                arguments_delta="{}",
            ),
        ),
        ProviderStreamEvent(type="done"),
    ]


def _write_catalog_plugin(source, *, version: str = "1.0.0") -> None:
    manifest_dir = source / ".ouro-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "plugin_id": "dev.ouro.catalog",
                "name": "Catalog",
                "version": version,
                "namespace": "catalog",
                "engines": {"ouro": ">=0.3.0,<0.4.0", "plugin_api": "^5.0.0"},
                "runtime": {
                    "kind": "python-inprocess",
                    "entrypoint": "catalog_plugin:create_plugin",
                },
                "contributes": {
                    "commands": [
                        {
                            "id": "generation",
                            "description": "Return the owning generation.",
                        }
                    ],
                    "tools": [
                        {
                            "id": "generation",
                            "exposed_name": "catalog__generation",
                            "description": "Return the owning generation.",
                        }
                    ],
                },
                "permissions": [],
                "dependencies": {"plugins": []},
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    (source / "catalog_plugin.py").write_text(
        """from ouro.sdk.plugins.v5 import (
    CommandContribution,
    CommandOutput,
    ContributionSet,
    ToolContribution,
    ToolOutput,
)

class Plugin:
    def __init__(self, context):
        self.generation_id = context.identity.generation_id

    async def prepare(self):
        async def generation(call):
            return ToolOutput(content=self.generation_id)
        async def generation_command(call):
            return CommandOutput(output=self.generation_id)
        return ContributionSet(
            tools=[ToolContribution(
                id='generation', exposed_name='catalog__generation',
                description='Return the owning generation.', handler=generation,
            )],
            commands=[CommandContribution(
                id='generation', description='Return the owning generation.',
                handler=generation_command,
            )],
        )

    async def start(self): pass
    async def stop(self, reason, deadline): pass

def create_plugin(context):
    return Plugin(context)
""",
        encoding="utf-8",
    )


def _write_resumable_plugin(source) -> None:
    manifest_dir = source / ".ouro-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "plugin_id": "dev.ouro.resume",
                "name": "Resume",
                "version": "1.0.0",
                "namespace": "resume",
                "engines": {"ouro": ">=0.3.0,<0.4.0", "plugin_api": "^5.0.0"},
                "runtime": {
                    "kind": "python-inprocess",
                    "entrypoint": "resume_plugin:create_plugin",
                },
                "contributes": {
                    "tools": [
                        {
                            "id": "workflow",
                            "exposed_name": "resume__workflow",
                            "description": "Resume an interactive workflow.",
                            "permission_mode": "auto",
                            "effect": "state_changing",
                            "batch_behavior": "exclusive",
                            "resumable": True,
                        }
                    ]
                },
                "permissions": [],
                "dependencies": {"plugins": []},
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    (source / "resume_plugin.py").write_text(
        """from ouro.sdk.plugins.v5 import (
    ContributionSet,
    ToolContribution,
    ToolInputRequest,
    ToolOutput,
)

class Plugin:
    def __init__(self, context):
        self.generation_id = context.identity.generation_id

    async def prepare(self):
        async def begin(call):
            return ToolInputRequest(data={
                'type': 'confirmation',
                'generation_id': self.generation_id,
            })
        async def resume(call):
            return ToolOutput(
                content='resumed',
                structured={
                    'answer': call.answer,
                    'generation_id': self.generation_id,
                },
            )
        return ContributionSet(tools=[ToolContribution(
            id='workflow', exposed_name='resume__workflow',
            description='Resume an interactive workflow.', permission_mode='auto',
            effect='state_changing', batch_behavior='exclusive',
            handler=begin, resume_handler=resume,
        )])

    async def start(self): pass
    async def stop(self, reason, deadline): pass

def create_plugin(context):
    return Plugin(context)
""",
        encoding="utf-8",
    )


def _write_skill_plugin(source) -> None:
    manifest_dir = source / ".ouro-plugin"
    skill_dir = source / "skills" / "calendar"
    manifest_dir.mkdir(parents=True)
    skill_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "plugin_id": "dev.ouro.skill_prompt",
                "name": "Skill Prompt",
                "version": "1.0.0",
                "namespace": "skill_prompt",
                "engines": {"ouro": ">=0.3.0,<0.4.0", "plugin_api": "^5.0.0"},
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
        "---\nname: calendar\ndescription: Manage the personal calendar.\n---\n\n"
        "Use calendar tools carefully.\n",
        encoding="utf-8",
    )
