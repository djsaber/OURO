from __future__ import annotations

import json
import shutil

import pytest

from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.core.adapter import RuntimeAdapter
from ouro.core.cancellation import AbortSignal
from ouro.core.provider import ModelMetadata, ProviderStreamEvent, ToolCallDelta
from ouro.core.tool_policies import (
    ToolPolicyDecision,
    ToolPolicyRegistration,
    ToolPolicyRegistry,
)
from ouro.core.tools import ToolInvocation
from ouro.local_application import LocalApplication
from ouro.plugins.api import adapt_sdk_contributions
from ouro.providers import FauxProvider
from ouro.sdk.plugins import v1 as sdk
from ouro.sessions import LocalSessionStore
from ouro.tools import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
    builtin_tools,
)
from ouro.tools.builtin_shell import detect_command_shell


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
        sdk.ContributionSet(
            tools=[
                sdk.ToolContribution(
                    id="inspect",
                    exposed_name="inspect_workspace",
                    description="Inspect the workspace context.",
                    handler=inspect_workspace,
                )
            ]
        )
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
    application.plugins.authorize("dev.ouro.guard")
    application.plugins.trust_code("dev.ouro.guard")
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
async def test_tool_controlled_tool_bypasses_host_permission_but_not_tool_policy(tmp_path) -> None:
    home = tmp_path / "home"
    store = LocalSessionStore(home)
    store.initialize()
    tools = ToolRegistry()

    async def control_harness(invocation) -> ToolResult:
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
                ProviderStreamEvent(
                    type="tool_call",
                    tool_call={
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
async def test_concurrent_runs_keep_their_immutable_plugin_catalog(tmp_path) -> None:
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
    application.plugins.authorize("dev.ouro.catalog")
    application.plugins.trust_code("dev.ouro.catalog")
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
    assert old_command.output == old_generation
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
    assert old_tool_result.text_content == old_generation
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
    first_application.plugins.authorize("dev.ouro.catalog")
    first_application.plugins.trust_code("dev.ouro.catalog")
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
    first_application.plugins.authorize("dev.ouro.resume")
    first_application.plugins.trust_code("dev.ouro.resume")
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
async def test_waiting_approval_rebuilds_retained_contract_after_plugin_update(
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
    first_application.plugins.authorize(
        "dev.ouro.catalog",
        package_digest=second.package_digest,
    )
    first_application.plugins.trust_code(
        "dev.ouro.catalog",
        package_digest=first.package_digest,
    )
    first_application.plugins.trust_code(
        "dev.ouro.catalog",
        package_digest=second.package_digest,
    )
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
    assert tool_result.text_content != selected_generation.generation_id
    assert (
        await second_application.plugins.remove(
            "dev.ouro.catalog",
            package_digest=first.package_digest,
        )
    ).package_digest == first.package_digest
    await second_application.close()


@pytest.mark.asyncio
async def test_waiting_approval_rejects_missing_retained_package(tmp_path) -> None:
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
    first_application.plugins.authorize("dev.ouro.catalog")
    first_application.plugins.trust_code("dev.ouro.catalog")
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

    with pytest.raises(RuntimeError, match="required plugin catalog is no longer available"):
        await second_runtime.approve_tool(
            session_id=waiting.session.session_id,
            run_id=waiting.run.run_id,
            approval_id=waiting.run.pending_approval_id,
            approved=True,
        )
    with pytest.raises(ValueError, match="retained by active durable runs"):
        await second_application.plugins.remove(
            "dev.ouro.catalog",
            package_digest=installed.package_digest,
        )
    await second_application.close()


@pytest.mark.asyncio
async def test_plugin_skill_prompt_is_resolved_from_each_run_catalog(tmp_path) -> None:
    class CapturingProvider(FauxProvider):
        def __init__(self) -> None:
            super().__init__()
            self.system_prompts: list[str] = []

        async def stream(self, *, model, system_prompt="", messages, tools=None):
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
        environment=RuntimeEnvironment(home=home),
    )
    application = LocalApplication.from_runtime(runtime, home=home)
    source = tmp_path / "skill-plugin"
    _write_skill_plugin(source)
    assert application.plugins is not None
    application.plugins.install(source)
    application.plugins.authorize("dev.ouro.skill_prompt")
    await application.start()
    assert (await application.plugins.enable("dev.ouro.skill_prompt")).status == "active"

    enabled_run = await runtime.run("use the calendar skill")
    assert enabled_run.run.status == "completed"
    assert "<name>calendar</name>" in provider.system_prompts[-1]
    assert "Manage the personal calendar." in provider.system_prompts[-1]

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
        and event.payload.get("phase") == "approval_modification"
    )
    assert reevaluation.payload["mode"] == "deny"


def _write_guard_plugin(source, command_tool_name: str) -> None:
    manifest_dir = source / ".ouro-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "plugin_id": "dev.ouro.guard",
                "name": "Guard",
                "version": "1.0.0",
                "namespace": "guard",
                "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^1.0.0"},
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
    plugin_source = """from ouro.sdk.plugins.v1 import (
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
                "schema_version": 2,
                "plugin_id": "dev.ouro.catalog",
                "name": "Catalog",
                "version": version,
                "namespace": "catalog",
                "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^1.0.0"},
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
        """from ouro.sdk.plugins.v1 import (
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
                "schema_version": 2,
                "plugin_id": "dev.ouro.resume",
                "name": "Resume",
                "version": "1.0.0",
                "namespace": "resume",
                "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^1.0.0"},
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
        """from ouro.sdk.plugins.v1 import (
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
                "schema_version": 2,
                "plugin_id": "dev.ouro.skill_prompt",
                "name": "Skill Prompt",
                "version": "1.0.0",
                "namespace": "skill_prompt",
                "engines": {"ouro": ">=0.1.0,<0.2.0", "plugin_api": "^1.0.0"},
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
