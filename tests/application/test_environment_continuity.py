from __future__ import annotations

import pytest

from ouro.application.agent_definitions import AgentDefinitionService
from ouro.application.runs import reserve_run
from ouro.application.runtime import RuntimeService
from ouro.application.session_controls import SessionControlCoordinator
from ouro.core.adapter import RuntimeAdapter
from ouro.core.errors import PromptRejectedError
from ouro.core.prompt_policies import (
    PromptPolicyDecision,
    PromptPolicyRegistration,
    PromptPolicyRegistry,
)
from ouro.core.provider import ModelMetadata, ProviderStreamEvent, ToolCallDelta
from ouro.core.session_controls import (
    AttributedSessionControlTransition,
    HostSessionInput,
    SessionControlClaim,
)
from ouro.core.state import RunRecord
from ouro.core.tool_policies import ToolPolicyDecision, ToolPolicyRegistration, ToolPolicyRegistry
from ouro.core.tools import ToolInputRequest, ToolInvocation
from ouro.plugins.catalog import ExtensionCatalogBase, build_extension_catalog
from ouro.plugins.session_controllers import (
    PluginSessionController,
    PluginSessionControllerOutput,
    PluginSessionControllerRegistry,
)
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult


class CurrentCatalog:
    """A publisher deliberately offering no historical reconstruction API."""

    def __init__(self, runtime, tools):
        self.runtime = runtime
        self.publish(tools)

    def publish(self, tools, *, tool_policies=None, prompt_policies=None, controllers=None):
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
        self.current = build_extension_catalog(
            ExtensionCatalogBase.capture(
                tools=registry,
                observers=self.runtime.observers,
                commands=None,
                compaction=None,
                continuations=None,
                tool_policies=tool_policies,
                prompt_policies=prompt_policies,
                session_controllers=controllers,
            ),
            (),
        )

    def snapshot(self):
        return self.current.acquire()


def request(name="action", *, call_id="call_action"):
    return [
        ProviderStreamEvent(
            type="tool_call_delta",
            tool_call_delta=ToolCallDelta(
                index=0, id_delta=call_id, name_delta=name, arguments_delta="{}"
            ),
        ),
        ProviderStreamEvent(type="done"),
    ]


def tool(handler, *, release="one", permission="auto", resumable=False):
    return Tool(
        definition=ToolDefinition(
            name="action",
            description="An operation with explicit implementation identity.",
            parameters_schema={"type": "object"},
            permission_mode=permission,
            resumable=resumable,
            metadata={
                "plugin_id": "example.action",
                "plugin_class": "user",
                "release_id": release,
                "package_digest": release,
                "contract_hash": release,
                "execution_contract_hash": release,
            },
        ),
        handler=handler,
        resume_handler=(
            lambda invocation, resolution, request, context: handler(invocation, context)
        )
        if resumable
        else None,
    )


@pytest.fixture(params=["files", "sqlite"])
def store(tmp_path, request):
    result = (
        LocalSessionStore(tmp_path / "sessions")
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    result.initialize()
    return result


@pytest.mark.parametrize("removed", [False, True])
async def test_approval_after_environment_change_closes_call_without_retargeting(store, removed):
    executed = []

    def execute(invocation, context):
        executed.append(invocation.tool_call_id)
        return ToolResult(tool_call_id=invocation.tool_call_id, tool_name="action", content="done")

    runtime = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    request(),
                    [
                        ProviderStreamEvent(type="text_delta", text="finished"),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
    )
    catalogs = CurrentCatalog(runtime, [tool(execute, permission="approval")])
    runtime.bind_extensions(catalog=catalogs)
    async with runtime:
        waiting = await runtime.run("perform the operation")
        assert waiting.run.status == "waiting_approval"
        old = catalogs.current
        assert old.lease_count == 0
        catalogs.publish([] if removed else [tool(execute, release="two", permission="approval")])
        result = await runtime.approve_tool(session_id=waiting.session.session_id, approved=True)
        assert result.run.status == "completed", result.run.error
        assert executed == []
        messages = store.load_runtime_session(waiting.session.session_id).messages
        results = [m.tool_result for m in messages if m.tool_result is not None]
        assert len(results) == 1
        assert results[0].tool_call_id == "call_action"
        assert results[0].is_error


async def test_host_tools_stay_bound_to_one_decision_and_refresh_for_the_next(store):
    executed = []
    seen = []
    registry = ToolRegistry()

    def implementation(label):
        def execute(invocation, context):
            executed.append(label)
            return ToolResult(
                tool_call_id=invocation.tool_call_id, tool_name="action", content=label
            )

        return Tool(
            definition=ToolDefinition(
                name="action",
                description=label,
                parameters_schema={"type": "object"},
                permission_mode="auto",
            ),
            handler=execute,
        )

    registry.register(implementation("first"))

    class UpdatingProvider:
        provider_id = "test"

        async def stream(self, *, tools, **kwargs):
            seen.append(next(item["description"] for item in tools if item["name"] == "action"))
            if len(seen) == 1:
                registry.replace(("action",), (implementation("second"),))
            if len(seen) < 3:
                for event in request(call_id=f"call_{len(seen)}"):
                    yield event
            else:
                yield ProviderStreamEvent(type="text_delta", text="finished")
                yield ProviderStreamEvent(type="done")

    runtime = RuntimeService(
        store=store,
        tools=registry,
        adapter=RuntimeAdapter(
            UpdatingProvider(), ModelMetadata(model_id="test", provider_id="test")
        ),
    )
    async with runtime:
        result = await runtime.run("perform two operations")
    assert result.run.status == "completed", result.run.error
    assert seen == ["first", "second", "second"]
    assert executed == ["first", "second"]


@pytest.mark.parametrize("resumable", [False, True])
async def test_missing_tool_preserves_answer_and_closes_interaction(store, resumable):
    runtime = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    request(),
                    [
                        ProviderStreamEvent(type="text_delta", text="finished"),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
    )
    catalogs = CurrentCatalog(
        runtime,
        [
            tool(
                lambda invocation, context: ToolInputRequest(data={"question": "choice?"}),
                resumable=resumable,
            )
        ],
    )
    runtime.bind_extensions(catalog=catalogs)
    async with runtime:
        waiting = await runtime.run("ask a question")
        assert waiting.run.status == "waiting_user"
        catalogs.publish([])
        result = await runtime.answer_user(
            session_id=waiting.session.session_id, run_id=waiting.run.run_id, answer="yes"
        )
        assert result.run.status == "completed", result.run.error
        receipt = next(event for event in result.events if event.event_type == "ask_user_resolved")
        assert receipt.payload["answer"] == "yes"
        messages = store.load_runtime_session(waiting.session.session_id).messages
        results = [message.tool_result for message in messages if message.tool_result is not None]
        assert len(results) == 1
        assert results[0].is_error is resumable
        if not resumable:
            assert (
                next(message.text_content for message in messages if message.tool_result) == "yes"
            )


async def test_next_model_round_observes_current_tools_and_executes_matching_binding(store):
    views = []

    class Provider(FauxProvider):
        async def stream(self, **kwargs):
            views.append([item["name"] for item in kwargs["tools"]])
            async for event in super().stream(**kwargs):
                yield event

    runtime = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(
            Provider(
                event_batches=[
                    request(),
                    [
                        ProviderStreamEvent(type="text_delta", text="finished"),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
    )

    def execute(invocation, context):
        catalogs.publish([])
        return ToolResult(tool_call_id=invocation.tool_call_id, tool_name="action", content="done")

    catalogs = CurrentCatalog(runtime, [tool(execute)])
    runtime.bind_extensions(catalog=catalogs)
    async with runtime:
        result = await runtime.run("perform the operation")
        assert result.run.status == "completed", result.run.error
        assert views == [["action"], []]


@pytest.mark.parametrize("policy", ["none", "permission", "tool_policy"])
async def test_answer_resume_checks_current_permission_and_checkpoints_before_effects(
    store, policy
):
    denied = policy != "none"
    executed = []

    def handler(invocation, context):
        return ToolInputRequest(data={"question": "continue?"})

    def resume(invocation, resolution, request, context):
        saved = store.load_run(invocation.session_id, invocation.run_id)
        call = saved.active_tool_batch.calls[0]
        assert call.status == "running"
        assert call.input_resolution.value == "yes"
        executed.append(invocation.tool_call_id)
        return ToolResult(tool_call_id=invocation.tool_call_id, tool_name="action", content="done")

    operation = tool(handler, resumable=True)
    operation.resume_handler = resume
    runtime = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    request(),
                    [
                        ProviderStreamEvent(type="text_delta", text="finished"),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
    )
    catalogs = CurrentCatalog(runtime, [operation])
    runtime.bind_extensions(catalog=catalogs)
    async with runtime:
        waiting = await runtime.run("ask")
        if policy == "permission":
            runtime.replace_tool_permission_overrides({"action": "deny"})
        elif policy == "tool_policy":
            policies = ToolPolicyRegistry()
            policies.replace_owner(
                "guard",
                (
                    ToolPolicyRegistration(
                        plugin_id="example.guard",
                        policy_id="deny",
                        handler=lambda call: ToolPolicyDecision(
                            action="deny", reason="Current policy"
                        ),
                    ),
                ),
            )
            catalogs.publish([operation], tool_policies=policies)
        result = await runtime.answer_user(
            session_id=waiting.session.session_id, run_id=waiting.run.run_id, answer="yes"
        )
        assert result.run.status == "completed", result.run.error
        assert executed == ([] if denied else ["call_action"])
        messages = store.load_runtime_session(waiting.session.session_id).messages
        tool_result = next(message.tool_result for message in messages if message.tool_result)
        assert tool_result.is_error is denied


async def test_delegated_capabilities_use_current_implementation_without_adding_tools(store):
    views = []

    class Provider(FauxProvider):
        async def stream(self, **kwargs):
            views.append([item["name"] for item in kwargs["tools"]])
            async for event in super().stream(**kwargs):
                yield event

    executed = []

    def handler(invocation, context):
        executed.append(invocation.release_id)
        return ToolResult(tool_call_id=invocation.tool_call_id, tool_name="action", content="done")

    runtime = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(
            Provider(
                event_batches=[
                    request(),
                    [
                        ProviderStreamEvent(type="text_delta", text="finished"),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
    )
    catalogs = CurrentCatalog(runtime, [tool(handler)])
    runtime.bind_extensions(catalog=catalogs)
    reference = AgentDefinitionService.tool_reference(
        catalogs.current.tools, catalogs.current.tools.get("action").definition
    )
    session = runtime.create_session(title="Delegated capability ceiling")
    run = RunRecord(
        session_id=session.session_id,
        status="queued",
        metadata={
            "effective_tool_names": ["action"],
            "effective_tool_refs": [reference.model_dump(mode="json")],
            "delegated_constraints": {"allow_external_paths": False},
        },
    )
    reserve_run(store, run)
    added = Tool(
        definition=ToolDefinition(name="extra", description="Not delegated"), handler=handler
    )
    catalogs.publish([tool(handler, release="two"), added])
    async with runtime:
        result = await runtime.run_prepared(
            run, HostSessionInput(prompt="perform", host_source_id="test")
        )
        assert result.run.status == "completed", result.run.error
        assert executed == ["two"]
        assert views == [["action"], ["action"]]


async def test_accepted_queue_survives_current_policy_rejection(store):
    def execute(invocation, context):
        return ToolResult(tool_call_id=invocation.tool_call_id, tool_name="action", content="done")

    runtime = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    request(),
                    [
                        ProviderStreamEvent(type="text_delta", text="finished"),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
    )
    operation = tool(execute, permission="approval")
    catalogs = CurrentCatalog(runtime, [operation])
    runtime.bind_extensions(catalog=catalogs)
    async with runtime:
        waiting = await runtime.run("first")
        await runtime.run("accepted input", session_id=waiting.session.session_id)
        pending = runtime.queued_inputs.eligible_session(
            store.load_session_record(waiting.session.session_id)
        )
        assert len(pending) == 1
        assert catalogs.current.lease_count == 0
        policies = PromptPolicyRegistry()
        policies.register(
            PromptPolicyRegistration(
                plugin_id="example.guard",
                policy_id="deny",
                handler=lambda call: PromptPolicyDecision(action="deny", reason="Current policy"),
            )
        )
        catalogs.publish([operation], prompt_policies=policies)
        with pytest.raises(PromptRejectedError, match="Current policy"):
            await runtime.approve_tool(session_id=waiting.session.session_id, approved=True)
        restored = store.load_runtime_session(waiting.session.session_id)
        assert restored.runs[0].status == "completed"
        assert runtime.queued_inputs.eligible_session(restored.session) == pending
        assert [
            message.text_content for message in restored.messages if message.role == "user"
        ] == ["first"]


async def test_newly_active_tool_policy_receives_current_harness_at_preflight(store):
    controller = PluginSessionController(
        controller_id="guard",
        description="Restrict tool execution",
        claims=(SessionControlClaim(domain="example.guard"),),
        activate_handler=lambda call: PluginSessionControllerOutput(),
        deactivate_handler=lambda call: PluginSessionControllerOutput(),
        plugin_id="example.guard",
        release_id="release",
        generation_id="generation",
        execution_contract_hash="contract",
    )
    controllers = PluginSessionControllerRegistry()
    controllers.replace_owner("guard", (controller,))
    observed = []

    def guard(call):
        observed.append(call.harness)
        return ToolPolicyDecision()

    policies = ToolPolicyRegistry()
    policies.replace_owner(
        "guard",
        (
            ToolPolicyRegistration(
                plugin_id="example.guard",
                policy_id="guard",
                handler=guard,
                active_when_controller_id=controller.canonical_id,
            ),
        ),
    )
    runtime = RuntimeService(
        store=store,
        tools=ToolRegistry(),
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
    )
    operation = tool(
        lambda invocation, context: ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name="action",
            content="done",
        )
    )
    catalogs = CurrentCatalog(runtime, [])
    catalogs.publish([operation], tool_policies=policies, controllers=controllers)
    runtime.bind_extensions(catalog=catalogs)
    session = runtime.create_session(title="Current controls")
    run = RunRecord(session_id=session.session_id)
    store.save_run(run)
    async with runtime:
        await runtime._ensure_extension_snapshot(run)
        coordinator = runtime._tool_calls_for_run(run.run_id)
        await SessionControlCoordinator(store).apply(
            AttributedSessionControlTransition(
                action="acquire",
                controller_id="guard",
                plugin_id="example.guard",
                generation_id="generation",
                execution_contract_hash="contract",
            ),
            session_id=session.session_id,
            controllers=controllers,
        )
        await coordinator._evaluate_tool_policies(
            [],
            0,
            session=session,
            run=run,
            phase="preflight",
            invocation=ToolInvocation(
                session_id=session.session_id,
                run_id=run.run_id,
                tool_name="action",
            ),
        )
    assert len(observed) == 1
    assert observed[0] is not None
    assert observed[0].state.controls.activations[0].controller_id == controller.canonical_id
