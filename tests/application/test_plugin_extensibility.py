from __future__ import annotations

import json

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
from ouro.tools import ToolExecutionContext, ToolRegistry, builtin_tools


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
    provider = FauxProvider(
        event_batches=[
            [
                ProviderStreamEvent(
                    type="tool_call_delta",
                    tool_call_delta=ToolCallDelta(
                        index=0,
                        id_delta="call_guarded",
                        name_delta="Bash",
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
    _write_guard_plugin(source)
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


def _write_guard_plugin(source) -> None:
    manifest_dir = source / ".ouro-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
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
    (source / "guard_plugin.py").write_text(
        """from ouro.sdk.plugins.v1 import (
    ContributionSet,
    ToolPolicyContribution,
    ToolPolicyDecision,
)

class Plugin:
    async def prepare(self):
        async def safe_shell(call):
            if call.tool_name == 'Bash' and 'unsafe' in str(call.arguments.get('command', '')):
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
""",
        encoding="utf-8",
    )
